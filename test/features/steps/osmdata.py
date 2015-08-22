import logging
from behave import *
from nose.tools import *
from random import random

from sqlalchemy import MetaData
from osgende.osmdata import OsmSourceTables
from geoalchemy2.elements import WKTElement
from osgende.common.nodestore import NodeStorePoint

logger = logging.getLogger(__name__)

def _insert_node(oid, tags, data, tables, conn):
    if data is None:
        pt = NodeStorePoint(random()*360 - 180, random()*180 - 90)
    else:
        x, y = data.split(' ', 2)
        pt = NodeStorePoint(float(x), float(y))
    if tables.nodestore is not None:
        tables.nodestore[oid] = pt


    if tables.nodestore is None or tags:
        conn.execute(
            tables.node.data.insert({'id' : oid, 'tags' : tags,
                                     'geom' : pt.wkb()}))

def _insert_way(oid, tags, nodes, tables, conn):
    nids = eval('[%s]' % nodes)
    conn.execute(tables.way.data.insert({'id' : oid, 'tags' : tags,
                                         'nodes' : nids }))

def _insert_rel(oid, tags, members, tables, conn):
    conn.execute(tables.relation.data.insert({'id' : oid, 'tags' : tags }))

    if members is None:
        return

    seq = 1
    for member in members.split(','):
        parts = member.split('/', 2)
        fullid = parts[0].strip()
        conn.execute(tables.member.data.insert(
                { 'relation_id' : oid,
                  'member_type' : fullid[0],
                  'member_id'   : int(fullid[1:]),
                  'member_role' : parts[1].strip() if len(parts) == 2 else None,
                  'sequence_id' : seq
                }))
        seq += 1

_insert_func = { 'N' : _insert_node, 'W' : _insert_way, 'R' : _insert_rel }

def _insert_element(row, tables, conn):
    tags = eval("{%s}" % row['tags']) if 'tags' in row.headings else None
    data = row['data'] if 'data' in row.headings else None

    _insert_func[row['id'][0]](int(row['id'][1:]), tags, data, tables, conn)

@given("the osm data")
def step_impl(context):
    meta = MetaData()
    context.osmdata = OsmSourceTables(meta, nodestore=context.nodestore_file)
    meta.create_all(context.engine)
    with context.engine.begin() as conn:
        for row in context.table:
            _insert_element(row, context.osmdata, conn)

@given("an update of osm data")
def step_impl(context):
    context.osmdata.node.change.delete()
    context.osmdata.way.change.delete()
    context.osmdata.relation.change.delete()
    with context.engine.begin() as conn:
        for row in context.table:
            tp = row['id'][0]
            if tp == 'N':
                src = context.osmdata.node
            elif tp == 'W':
                src = context.osmdata.way
            elif tp == 'R':
                src = context.osmdata.relation
            else:
                assert(False)
            oid = int(row['id'][1:])
            # change table
            conn.execute(src.change.insert({'action' : row['action'], 'id' : oid}))
            if row['action'] in ('D', 'M'):
                conn.execute(src.data.delete().where(src.data.c.id == oid))
            if row['action'] in ('M', 'A'):
                _insert_element(row, context.osmdata, conn)
