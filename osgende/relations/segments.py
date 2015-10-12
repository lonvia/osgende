# This file is part of Osgende
# Copyright (C) 2015 Sarah Hoffmann
#
# This is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

import logging

from sqlalchemy import Table, Column, BigInteger, select, Index, or_, bindparam,\
                       case, union, text, column
from sqlalchemy.sql import functions as sqlf
from sqlalchemy.dialects.postgresql import ARRAY, array
from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_Transform
from geoalchemy2.shape import from_shape
from osgende.common.sqlalchemy import CreateTableAs
from osgende.common.threads import ThreadableDBObject
from sys import version_info as python_version
import threading
from osgende.common.geom import FusableWay
from osgende.subtable import TagSubTable
from osgende.common.sqlalchemy import DropIndexIfExists
import osgende.common.threads as othreads
import shapely.geometry as sgeom

log = logging.getLogger(__name__)

class RouteSegments(object):
    """ Collects the geometries of route relations in a network.

        Segments are the basic way system of the network. They are the longest
        linear pieces in the network that do not overlap and have the same
        set of routes going over them.

        This table only collects the information about the network geometry.
        If additional information needs to be stored, you should create an
        additional table and connect the two via the id primary key.
    """

    def __init__(self, meta, name, osmtables,
                 subset=None, srid=None, geom_change=None):
        if isinstance(subset, str):
            self.subset = text(subset)
        else:
            self.subset = subset
        self.geom_change = geom_change
        self.osmtables = osmtables
        self.meta = meta
        self.numthreads = None

        if srid is None:
            srid = meta.info.get('srid', osmtables.node.data.c.geom.type.srid)

        self.data = Table(name, meta,
                          Column('id', BigInteger, primary_key=True),
                          Column('nodes', ARRAY(BigInteger)),
                          Column('ways', ARRAY(BigInteger)),
                          Column('rels', ARRAY(BigInteger)),
                          Column('geom', Geometry('LINESTRING', srid=srid))
                         )

    def set_num_threads(self, num):
        self.numthreads = num

    def truncate(self, engine):
        engine.execute(self.data.delete())

    def _compute_first(self, conn):
        cur_id = conn.scalar(select([sqlf.max(self.data.c.id)]))
        if cur_id is None:
            self.first_new_id = 0
        else:
            self.first_new_id = cur_id + 1

    def construct(self, engine):
        """Collect all segments.
        """
        t = self.osmtables.member.data
        stm_get_ways = select([t.c.member_id])\
                         .where(t.c.member_type == 'W')\
                         .where(t.c.relation_id == bindparam('id'))\
                         .compile(engine)
        with engine.begin() as conn:
            self._compute_first(conn)

            # manual indexes
            relidx = Index("%s_rels_idx" % (self.data.name),
                           self.data.c.rels, postgresql_using='gin')
            wayidx = Index("%s_ways_idx" % (self.data.name),
                           self.data.c.ways, postgresql_using='gin')
            # drop indexes if any
            conn.execute(DropIndexIfExists(relidx))
            conn.execute(DropIndexIfExists(wayidx))

            self.truncate(conn)

            wayproc = _WayCollector(self, engine, creation_mode=True,
                                    numthreads=self.numthreads)

        with engine.begin() as conn:
            sortedrels = list(wayproc.relations)
            sortedrels.sort()
            todo = len(sortedrels)
            done = 0
            for rel in sortedrels:
                log.log(logging.INFO if done % 100 == 0 else logging.DEBUG,
                        "Processing relation %d (%d %%)", rel, done * 100 / todo)
                ways = conn.execute(stm_get_ways, { 'id' : rel })
                for w in ways:
                    wayproc.add_way(conn, w[0])

                # Put the ways collected so far into segments
                wayproc.process_segments()
                done += 1

            wayproc.finish()

            # finally prepare indices to speed up update
            relidx.create(conn)
            wayidx.create(conn)


    def update(self, engine):
        """Update changed segments.
        """
        self._compute_first(engine)
        wayproc = _WayCollector(self, engine, precompute_intersections=False)
        log.debug("Valid relations:", wayproc.relations)

        with engine.begin() as conn:
            log.info("Collecting changed and new ways")
            wt = self.osmtables.way.data
            mt = self.osmtables.member.data
            rt = self.osmtables.relation.data
            sel = select([wt.c.id, wt.c.nodes]).where(wt.c.id.in_(
                        select([mt.c.member_id])
                          .where(rt.c.id == mt.c.relation_id)
                          .where(mt.c.member_type == 'W')
                          .where(self.subset)
                          .where(or_(mt.c.relation_id.in_(
                                     self.osmtables.relation.select_add_modify()),
                                     mt.c.member_id.in_(
                                     self.osmtables.way.select_modify())))
                  ))
            conn.execute(CreateTableAs('temp_updated_ways', sel))
            temp_ways = Table('temp_updated_ways', self.meta, autoload_with=conn)

            log.info("Adding those ways to changeset")
            res = conn.execute(temp_ways.select())
            for c in res:
                wayproc.add_way(conn, c['id'], c['nodes'])

            log.info("Collecting points effected by update")
            # collect all nodes that are affected by the update:
            #  1. nodes in segments whose relation or ways have changed
            waysel = select([self.osmtables.way.change.c.id])
            relsel = select([self.osmtables.relation.change.c.id])
            segchg = select([sqlf.func.unnest(text('ARRAY[nodes[1],nodes[array_length(nodes,1)]]')).label('id')])\
                      .where(or_(
                          self.data.c.ways.op('&& ARRAY')(waysel),
                          self.data.c.rels.op('&& ARRAY')(relsel)
                            ))
            #  2. nodes in added or changed ways
            waychg = select([sqlf.func.unnest(temp_ways.c.nodes)])
            #  3. nodes that have been moved
            ndchg = self.osmtables.node.select_modify()

            conn.execute(CreateTableAs('temp_updated_nodes',
                                       union(segchg, waychg, ndchg).alias('sub')))
            temp_nodes = Table('temp_updated_nodes', self.meta, autoload_with=conn)

            log.debug("Nodes needing updating:",
                      self.select_column("SELECT * FROM temp_updated_nodes"))

            # create a temporary function that scans our temporary
            # node table. This is hopefully faster than a full cross scan.
            conn.execute("""
                  CREATE OR REPLACE FUNCTION temp_updated_nodes_find(a ANYARRAY)
                  RETURNS bool AS
                      $$
                        DECLARE
                          ele bigint;
                        BEGIN
                         FOR ele IN SELECT unnest(a) LOOP
                           PERFORM * FROM temp_updated_nodes WHERE id = ele LIMIT 1;
                           IF FOUND THEN RETURN true; END IF;
                         END LOOP;
                         RETURN false;
                        END
                        $$
                        LANGUAGE plpgsql;
                       CREATE INDEX temp_updated_nodes_index ON temp_updated_nodes(id);
                   """)
            # throw out all segments that have one of these points
            log.info("Segments with bad intersections...")
            ret = [self.data.c.ways]
            if self.geom_change is not None:
                ret.append(self.data.c.geom)
            res = conn.execute(self.data.delete()
                                 .where("temp_updated_nodes_find(nodes)")
                                 .returning(*ret))
            for c in res:
                for w in c['ways']:
                    wayproc.add_way(conn, w)
                if self.geom_change is not None:
                    self.geom_change.add(c['geom'], 'D')

            conn.execute("DROP FUNCTION temp_updated_nodes_find(ANYARRAY)")

            # done, add the result back to the table
            log.info("Processing segments")
            wayproc.process_segments()
            wayproc.finish()

            # add all newly created segments to the update table
            if self.geom_change is not None:
                self.geom_change.add_from_select(
                      select([ text("'M'"), self.data.c.geom])
                        .where(self.data.c.id >= self.first_new_id))


class _WayCollector(ThreadableDBObject):
    """Collects a bunch of fusable ways and orders them by
       relation they belong to. If the collector is in creation
       mode, it will do some optimations with respect to remembering
       which ways have already been processed. To be more detailed:
       it will only remember to not process again ways that are member
       of multiple relations.
    """

    def __init__(self, parent, engine,
                  creation_mode=False, precompute_intersections=True,
                  numthreads=None):
        self.src = parent
        self.not_creation_mode = not creation_mode
        self.intersections_from_ways = not precompute_intersections
        self._get_intersections(engine)
        self.src_srid = parent.osmtables.node.data.c.geom.type.srid
        self.needs_transform = parent.data.c.geom.type.srid != self.src_srid

        # Next get the set of relevant relations
        r = self.src.osmtables.relation.data
        self.relations = set()
        res = engine.execute(select([r.c.id]).where(self.src.subset))
        for r in res:
            self.relations.add(r['id'])

        if self.relations is None:
            log.warning("WARNING: no relevant relations found")
        else:
            self.relations = set(self.relations)

        # ways already processed
        self.waysdone = set()

        # actual collection of fusable ways
        self.relgroups = {}

        # the worker threads
        self.set_num_threads(numthreads)
        self.workers = self.create_worker_queue(engine, self._process_next)

        # prepare the SQL we are going to need
        m = self.src.osmtables.member.data
        self._stm_way_rels = select([m.c.relation_id, m.c.member_role])\
                               .where(m.c.member_type == 'W')\
                               .where(m.c.member_id == bindparam('id'))\
                               .order_by(m.c.relation_id).compile(engine)

        w = self.src.osmtables.way.data
        self._stm_way_nodes = select([w.c.nodes])\
                                .where(w.c.id == bindparam('id'))\
                                .compile(engine)

    def add_way(self, conn, way, nodes=None):
        """Add another OSM way according to its relations.
        """
        if way in self.waysdone:
           return

        # Determine the set of relation/role pairs for each way
        wcur = conn.execute(self._stm_way_rels, { 'id' : way })
        membership = []
        for c in wcur:
            if c[0] in self.relations:
                membership.append((c[0], c[1]))
        if len(membership) == 0:
            return
        # We actually only need to remember ways with more than one
        # relation on them. All others are not expected to come up again.
        #if self.not_creation_mode or len(membership) > 1:
        self.waysdone.add(way)

        membership = tuple(membership)
        # get the nodes
        if nodes is None:
            nodes = conn.scalar(self._stm_way_nodes, { 'id' : way })

        if nodes:
            # remove duplicated nodes if they immediately follow each other
            # This needs to be done to resolve an as of yet unresolved Potlach
            # bug, see: http://trac.openstreetmap.org/ticket/2501
            for i in range(len(nodes)-1,0,-1):
                if nodes[i] == nodes[i-1]: del nodes[i]
            if not membership in self.relgroups:
                self.relgroups[membership] = _SegmentCollector(self.intersections)
            self.relgroups[membership].add(way,nodes)
            # update intersections
            if self.intersections_from_ways:
                self._add_intersection(nodes[0], 1)
                if len(nodes) > 1:
                    for n in nodes[1:-1]:
                        self._add_intersection(n, 2)
                    self._add_intersection(nodes[-1], 1)

        else:
            log.debug("No nodes. Dropped")

    def _add_intersection(self, nid, weight):
        if nid in self.collected_nodes:
            self.collected_nodes[nid] += weight
        else:
            self.collected_nodes[nid] = weight


    def process_segments(self):
        """Fuse and write out the ways collected so far.
        """
        if self.intersections_from_ways:
            if python_version[0] < 3:
                self.processing_intersections = set([k for (k,v) in self.collected_nodes.iteritems() if v > 2])
            else:
                self.processing_intersections = set([k for (k,v) in self.collected_nodes.items() if v > 2])

        else:
            self.processing_intersections = None

        while self.relgroups:
            (rels, collector) = self.relgroups.popitem()
            relids = [x for (x,y) in rels]
            self.workers.add_task((relids, collector))

    def _process_next(self, item):
        (relids, collector) = item
        collector.make_segments(intersections=self.processing_intersections)
        for w in collector.ways:
            self._write_segment(w, relids)

    def finish(self):
        """Finish up any pending operations.
           Needs only to be called once after all segments have been processed.
        """
        self.workers.finish()
        del self.intersections

    def _get_intersections(self, engine):
        """ Find all potential mid-way intersections.
        """
        self.intersections = set()

        if self.intersections_from_ways:
            self.collected_nodes = {}
            return

        # Explanation of that nasty bit of SQL:
        # Count for each node in how many ways that are part of
        # relevant relations it appears. Start and end nodes count only
        # half because we don't care about two ways meeting if they have
        # the same attributes.

        w = self.src.osmtables.way.data
        r = self.src.osmtables.relation.data
        m = self.src.osmtables.member.data
        # first find the ways that are part in any of the interesting relations
        way_ids = select([m.c.member_id]).distinct()\
                    .where(m.c.member_type == 'W')\
                    .where(m.c.relation_id.in_(
                                select([r.c.id]).where(self.src.subset)))
        # create a list of nodes with their position in the way
        nodelist = select([w.c.nodes,
                           sqlf.func.generate_subscripts(w.c.nodes, 1).label('i')])\
                     .where(w.c.id.in_(way_ids)).alias("nodelist")

        # weigh each node by position (front, middle, end)
        wei = select([nodelist.c.nodes[nodelist.c.i].label('nid'),
                     case([(or_(nodelist.c.i == 1,
                                nodelist.c.i == sqlf.func.array_length(nodelist.c.nodes, 1)),
                            1)],
                          else_ = 2).label('w')
                     ]).alias('weighted')
        # sum up the weights for each node
        total = select([wei.c.nid.label('nid'), sqlf.sum(wei.c.w).label('sum')])\
                  .group_by(wei.c.nid).alias('total')

        # anything with weight larger than 1 must be a real intersection
        c = engine.execute(select([total.c.nid]).where(total.c.sum > 2))

        for ele in c:
            self.intersections.add(ele['nid'])

        log.debug("Final intersections:", self.intersections)


    def _write_segment(self, way, relations):
        # get the node geometries and the countries
        countries = {}
        prevpoints = (0,0)

        points = self.src.osmtables.get_points(way.nodes)

        # ignore ways where the node geometries are missing
        if len(points) > 1:
            line = from_shape(sgeom.LineString(points), self.src_srid)

            if self.needs_transform:
                line = ST_Transform(line, self.src.data.c.geom.type.srid)

            self.thread.conn.execute(self.src.data.insert(
                 { 'nodes' : way.nodes, 
                   'ways' : way.ways,
                   'rels' : relations,
                   'geom' : line }))

            #self.db.commit()
        else:
            log.warning("empty way: %s", way)




class _SegmentCollector:
    """Helper class that collects ways of the same kind and
       creates subequent segments from it.

       'intersections' are nodes where a way must forcably broken up.
    """

    def __init__(self, intersections):
        self.intersections = intersections
        self.ways = set()
        self.pointlist = {}

    def add(self, way, nodes):
        """Add a new way to the list.

           Should any of the middle nodes be an intersection then
           the way is split at this point before adding it to the list.
        """
        # find all nodes that are forced intersecions
        splitidx = [x for x in range(len(nodes))
                     if nodes[x] in self.intersections]
        if len(splitidx) == 0 or splitidx[0] != 0:
            splitidx[:0] = [0]
        if splitidx[-1] != len(nodes)-1:
            splitidx.append(len(nodes)-1)
        for i in range(len(splitidx)-1):
            w = FusableWay(way, nodes[splitidx[i]:splitidx[i+1]+1])
            self.ways.add(w)
            for n in (w.first(),w.last()):
                if n in self.pointlist:
                    self.pointlist[n].append(w)
                else:
                    self.pointlist[n] = [w]


    def make_segments(self, intersections=None):
        """Fuse ways as much as possible.
        """
        if intersections is not None:
            self.intersections=intersections
            self.split_ways()
        for (node,ways) in self.pointlist.items():
            if len(ways) == 2 and not node in self.intersections:
                (w1,w2) = ways
                if w1 != w2:
                    othernode = w1.fuse(w2, node)
                    otherways = self.pointlist[othernode]
                    for i in range(len(otherways)):
                        if otherways[i] == w2:
                            otherways[i] = w1
                    self.ways.remove(w2)

    def split_ways(self):
        """Rebuild the waylist, splitting all ways on
           forced intersections. This will only work if
           all segments in the collector are part of exactly
           one way.
        """
        oldways = self.ways
        self.ways = set()
        self.pointlist = {}
        for w in oldways:
            assert(len(w.ways)==1)
            self.add(w.ways[0], w.nodes)


class Routes(TagSubTable):
    """A relation collection class that gets updated according to
       the changes in a RouteSegments table. If an optional hierarchy
       table is provided, super relations will be updated as well.
    """

    def __init__(self, name, segments, hiertable=None):
        self.segment_table = segments
        self.hierarchy_table = hiertable
        TagSubTable.__init__(self, segments.meta, name,
                             segments.osmtables.relation,
                             subset=segments.subset)


    def update(self, engine):
        firstid = self.segment_table.first_new_id

        with engine.begin() as conn:
            # delete any objects that might have been deleted
            # (Note: a relation also might get deleted from this table
            # because it lost its relevant tags
            conn.execute(self.data.delete().where(self.id_column.in_
                                            (self.src.select_modify_delete())))
            # Collect all changed relations in a temporary table
            sel = select([sqlf.func.unnest(self.segment_table.data.c.rels)], distinct=True)\
                       .where(self.segment_table.data.c.id >= firstid)

            if self.hierarchy_table is not None:
                sel = select([self.hierarchy_table.data.c.parent], distinct=True)\
                       .where(self.hierarchy_table.data.c.child.in_(
                                sel.union(self.src.select_add_modify())))

            conn.execute(CreateTableAs('__tmp_osgende_routes_updaterels', sel))
            tmp_rels = Table('__tmp_osgende_routes_updaterels',
                             self.segment_table.meta, autoload_with=conn)

            conn.execute(self.data.delete()\
                           .where(self.id_column.in_(tmp_rels.select())))

        # reinsert those that are not deleted
        inssel = self.src.select_all(self.src.data.c.id.in_(tmp_rels.select()))
        self.insert_objects(engine, inssel)
        # drop the temporary table
        tmp_rels.drop(engine)

