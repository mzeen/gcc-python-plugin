#   Copyright 2012 David Malcolm <dmalcolm@redhat.com>
#   Copyright 2012 Red Hat, Inc.
#
#   This is free software: you can redistribute it and/or modify it
#   under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful, but
#   WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#   General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see
#   <http://www.gnu.org/licenses/>.

import gcc

from gccutils.dot import to_html

############################################################################
# Generic directed graphs
############################################################################
class Graph:
    def __init__(self):
        self.nodes = []
        self.edges = []

    def add_node(self, node):
        self.nodes.append(node)
        return node

    def add_edge(self, srcnode, dstnode, *args, **kwargs):
        assert isinstance(srcnode, Node)
        assert isinstance(dstnode, Node)
        e = self._make_edge(srcnode, dstnode, *args, **kwargs)
        self.edges.append(e)
        srcnode.succs.append(e)
        dstnode.preds.append(e)
        return e

    def _make_edge(self, srcnode, dstnode):
        return Edge(srcnode, dstnode)

    def to_dot(self, name, ctxt=None):
        result = 'digraph %s {\n' % name
        result += '  node [shape=box];\n'
        result += self._nodes_to_dot(ctxt)
        result += self._edges_to_dot(ctxt)
        result += '}\n'
        return result

    def _nodes_to_dot(self, ctxt):
        # 1st pass: split nodes out by subgraph:
        from collections import OrderedDict
        subgraphs = OrderedDict()
        for node in self.nodes:
            subgraph = node.get_subgraph(ctxt)
            if subgraph in subgraphs:
                subgraphs[subgraph].append(node)
            else:
                subgraphs[subgraph] = [node]

        # 2nd pass: render the subgraphs (and the "None" subgraph, at the
        # top level):
        result = ''
        for subgraph in subgraphs:
            if subgraph:
                result += '  subgraph cluster_%s {\n' % subgraph
            for node in subgraphs[subgraph]:
                result += ('  %s [label=<%s>];\n'
                           % (node.to_dot_id(),
                              node.to_dot_label(ctxt)))
            if subgraph:
                result += '  }\n'

        return result

    def _edges_to_dot(self, ctxt):
        result = ''
        for edge in self.edges:
            result += ('    %s -> %s [label=<%s>%s];\n'
                       % (edge.srcnode.to_dot_id(),
                          edge.dstnode.to_dot_id(),
                          edge.to_dot_label(ctxt),
                          edge.to_dot_attrs(ctxt)))
        return result

    def get_shortest_path(self, srcnode, dstnode):
        '''
        Locate the shortest path from the srcnode to the dstnode
        Return a list of Edge instances, or None if no such path exists
        '''
        # Dijkstra's algorithm
        # A dict giving for each node the length of the shortest known path
        # from srcnode to this node:
        distance = {}

        # A dict giving for each node the previous node within that shortest
        # path:
        inedge = {}

        INFINITY = 0x80000000
        for node in self.nodes:
            distance[node] = INFINITY
            inedge[node] = None
        distance[srcnode] = 0

        worklist = list(self.nodes)
        while worklist:
            # we don't actually need to do a full sort each time, we could
            # just update the position of the item that changed
            worklist.sort(lambda node1, node2:
                              distance[node1] - distance[node2])
            node = worklist[0]
            if node == dstnode:
                # We've found the target node; build a path of the edges to
                # follow to get here:
                path = []
                while inedge[node]:
                    path = [inedge[node]] + path
                    node = inedge[node].srcnode
                return path
            worklist = worklist[1:]
            if distance[node] == INFINITY:
                # disjoint
                break
            for edge in node.succs:
                alt = distance[node] + 1
                if alt < distance[edge.dstnode]:
                    distance[edge.dstnode] = alt
                    inedge[edge.dstnode] = edge



class Node:
    def __init__(self):
        self.preds = []
        self.succs = []

    def to_dot_id(self):
        return '%s' % id(self)

    def to_dot_label(self, ctxt):
        node = self.to_dot_html(ctxt)
        if node:
            return node.to_html()
        else:
            return to_html(str(self))

    def to_dot_html(self, ctxt):
        # Optionally, build a tree of gccutils.dot.Node
        return None

    def get_subgraph(self, ctxt):
        # Optionally, allow nodes to be partitioned into subgraphs (by name)
        return None

class Edge:
    def __init__(self, srcnode, dstnode):
        self.srcnode = srcnode
        self.dstnode = dstnode

    def __repr__(self):
        return '%s(srcnode=%r, dstnode=%r)' % (self.__class__.__name__, self.srcnode, self.dstnode)

    def __str__(self):
        return '%s -> %s' % (self.srcnode, self.dstnode)

    def to_dot_label(self, ctxt):
        return ''

    def to_dot_attrs(self, ctxt):
        return ''

############################################################################
# A CFG, but with individual statements for nodes, rather than lumping them
# together within basic blocks
# It also has "empty" nodes i.e. those with no statements, to handle
# the empty BBs in the original CFG (entry and exit)
# FIXME: this doesn't yet cover PHI nodes...
############################################################################
class StmtGraph(Graph):
    def __init__(self, fun, split_phi_nodes):
        """
        fun : the underlying gcc.Function

        split_phi_nodes:

           if true, split phi nodes so that there is one copy of each phi
           node per edge as a SplitPhiNode instance, allowing client code
           to walk the StmtGraph without having to track which edge we came
           from

           if false, create a StmtNode per phi node at the top of the BB

        """
        Graph.__init__(self)
        self.fun = fun
        self.entry = None
        self.exit = None
        # Mappings from gcc.BasicBlock to StmtNode so that we can wire up
        # the edges for the gcc.Edge:
        self.entry_of_bb = {}
        self.exit_of_bb = {}
        self.node_for_stmt = {}

        # 1st pass: create nodes and edges within BBs:
        for bb in fun.cfg.basic_blocks:
            self.__lastnode = None

            def add_stmt(stmt):
                nextnode = self.add_node(StmtNode(fun, stmt))
                self.node_for_stmt[stmt] = nextnode
                if self.__lastnode:
                    self.add_edge(self.__lastnode, nextnode, None)
                else:
                    self.entry_of_bb[bb] = nextnode
                self.__lastnode = nextnode

            if bb.phi_nodes and not split_phi_nodes:
                # If we're not splitting the phi nodes, add them to the top
                # of each BB:
                for stmt in bb.phi_nodes:
                    add_stmt(stmt)
                self.exit_of_bb[bb] = self.__lastnode
            if bb.gimple:
                for stmt in bb.gimple:
                    add_stmt(stmt)
                self.exit_of_bb[bb] = self.__lastnode

            if self.__lastnode is None:
                if bb == fun.cfg.entry:
                    cls = EntryNode
                elif bb == fun.cfg.exit:
                    cls = ExitNode
                else:
                    # gcc appears to create empty BBs for functions
                    # returning void that contain multiple "return;"
                    # statements:
                    cls = StmtNode
                node = self.add_node(cls(fun, None))
                self.entry_of_bb[bb] = node
                self.exit_of_bb[bb] = node
                if bb == fun.cfg.entry:
                    self.entry = node
                elif bb == fun.cfg.exit:
                    self.exit = node

        # 2nd pass: wire up the cross-BB edges:
        for bb in fun.cfg.basic_blocks:
            for edge in bb.succs:
                last_node = self.exit_of_bb[bb]
                if split_phi_nodes:
                    # add SplitPhiNode instances at the end of each edge
                    # as a copy of each phi node, specialized for this edge
                    if edge.dest.phi_nodes:
                        for stmt in edge.dest.phi_nodes:
                            split_phi = self.add_node(SplitPhiNode(fun, stmt, edge))
                            self.add_edge(last_node,
                                          split_phi,
                                          edge)
                            last_node = split_phi
                self.add_edge(last_node,
                              self.entry_of_bb[edge.dest],
                              edge)

    def _make_edge(self, srcnode, dstnode, edge):
        return StmtEdge(srcnode, dstnode, edge)

    def get_entry_nodes(self):
        return [self.entry]

class StmtNode(Node):
    def __init__(self, fun, stmt):
        Node.__init__(self)
        self.fun = fun
        self.stmt = stmt # can be None for empty BBs

    def __str__(self):
        return str(self.stmt)

    def __repr__(self):
        return 'StmtNode(%r)' % self.stmt

    def get_stmt(self):
        return self.stmt

    def get_gcc_loc(self):
        if self.stmt:
            return self.stmt.loc
        else:
            return None

    def to_dot_html(self, ctxt):
        from gccutils.dot import Table, Tr, Td, Text, Br, Font
        from gccutils import get_src_for_loc

        loc = self.get_gcc_loc()
        if loc:
            table = Table()
            code = get_src_for_loc(loc).rstrip()
            tr = table.add_child(Tr())
            td = tr.add_child(Td(align='left'))
            td.add_child(Text('%4i %s' % (self.stmt.loc.line, code)))
            td.add_child(Br())
            td.add_child(Text(' ' * (5 + self.stmt.loc.column-1) + '^'))
            td.add_child(Br())
            td.add_child(Text(str(self)))
            return table
            # return Font([table], face="monospace")
        else:
            return Text(str(self))

class EntryNode(StmtNode):
    def to_dot_html(self, ctxt):
        from gccutils.dot import Table, Tr, Td, Text

        funtype = self.fun.decl.type
        args = ','.join(['%s %s' % (arg.type, arg.name)
                         for arg in self.fun.decl.arguments])
        signature = '%s %s(%s)' % (funtype.type, self.fun.decl.name, args)

        table = Table([
            Tr([
                Td([
                    Text('ENTRY %s' % signature)
                    ])
                ])
            ])
        for var in self.fun.local_decls:
            table.add_child(Tr([
                        Td([
                                Text('%s %s;' % (var.type, var))
                                ])
                        ]))
        return table

    def __str__(self):
        return 'ENTRY %s' % self.fun.decl.name

    def __repr__(self):
        return 'EntryNode(%r)' % self.fun.decl.name

class ExitNode(StmtNode):
    def __str__(self):
        return 'EXIT %s' % self.fun.decl.name

    def __repr__(self):
        return 'ExitNode(%r)' % self.fun.decl.name

    @property
    def returnnode(self):
        """
        Get the gcc.GimpleReturn statement associated with this function exit
        """
        assert len(self.preds) == 1
        node = self.preds[0].srcnode
        assert isinstance(node.stmt, gcc.GimpleReturn)
        return node

class SplitPhiNode(StmtNode):
    def __init__(self, fun, stmt, inneredge):
        StmtNode.__init__(self, fun, stmt)
        self.inneredge = inneredge

        # Lookup the RHS for this edge:
        assert isinstance(stmt, gcc.GimplePhi)
        assert isinstance(inneredge, gcc.Edge)
        self.rhs = None
        for arg, edge in stmt.args:
            if edge == inneredge:
                self.rhs = arg
                break
        if self.rhs is None:
            raise UnknownEdge()

    def __str__(self):
        return '%s via %s' % (self.stmt, self.inneredge)

    def __repr__(self):
        return 'SplitPhiNode(%r, %r)' % (self.stmt, self.inneredge)

class StmtEdge(Edge):
    def __init__(self, srcnode, dstnode, cfgedge):
        Edge.__init__(self, srcnode, dstnode)
        self.cfgedge = cfgedge # will be None within a BB

    def to_dot_label(self, ctx):
        if self.cfgedge:
            if self.cfgedge.true_value:
                return 'true'
            elif self.cfgedge.false_value:
                return 'false'
        return ''

    @property
    def true_value(self):
        if self.cfgedge:
            return self.cfgedge.true_value

    @property
    def false_value(self):
        if self.cfgedge:
            return self.cfgedge.false_value

############################################################################
# Supergraph of all CFGs, built from each functions' StmtGraph.
# A graph in which the nodes wrap StmtNode
############################################################################
class Supergraph(Graph):
    def __init__(self, split_phi_nodes):
        Graph.__init__(self)
        # 1st pass: locate interprocedural instances of gcc.GimpleCall
        # i.e. where both caller and callee are within the supergraph
        # (perhaps the same function)
        ipcalls = set()
        from gcc import get_callgraph_nodes
        for node in get_callgraph_nodes():
            fun = node.decl.function
            if fun:
                for edge in node.callees:
                    if edge.callee.decl.function:
                        ipcalls.add(edge.call_stmt)

        # 2nd pass: construct a StmtGraph for each function in the callgraph
        # and add nodes and edges to "self" wrapping the nodes and edges
        # within each StmtGraph:
        self.stmtg_for_fun = {}
        for node in get_callgraph_nodes():
            fun = node.decl.function
            if fun:
                stmtg = StmtGraph(fun, split_phi_nodes)
                self.stmtg_for_fun[fun] = stmtg
                # Clone the stmtg nodes and edges into the Supergraph:
                stmtg.supernode_for_stmtnode = {}
                for node in stmtg.nodes:
                    if node.stmt in ipcalls:
                        # These nodes will have two supernodes, a CallNode
                        # and a ReturnNode:
                        callnode = self.add_node(CallNode(node, stmtg))
                        returnnode = self.add_node(ReturnNode(node, stmtg))
                        stmtg.supernode_for_stmtnode[node] = (callnode, returnnode)
                        self.add_edge(
                            callnode, returnnode,
                            CallToReturnSiteEdge, None)
                    else:
                        stmtg.supernode_for_stmtnode[node] = \
                            self.add_node(SupergraphNode(node, stmtg))
                for edge in stmtg.edges:
                    if edge.srcnode.stmt in ipcalls:
                        # Begin the superedge from the ReturnNode:
                        srcsupernode = stmtg.supernode_for_stmtnode[edge.srcnode][1]
                    else:
                        srcsupernode = stmtg.supernode_for_stmtnode[edge.srcnode]
                    if edge.dstnode.stmt in ipcalls:
                        # End the superedge at the CallNode:
                        dstsupernode = stmtg.supernode_for_stmtnode[edge.dstnode][0]
                    else:
                        dstsupernode = stmtg.supernode_for_stmtnode[edge.dstnode]
                    superedge = self.add_edge(srcsupernode, dstsupernode,
                                              SupergraphEdge, edge)

        # 3rd pass: add the interprocedural edges (call and return):
        for node in get_callgraph_nodes():
            fun = node.decl.function
            if fun:
                for edge in node.callees:
                    if edge.callee.decl.function:
                        calling_stmtg = self.stmtg_for_fun[fun]
                        called_stmtg = self.stmtg_for_fun[edge.callee.decl.function]

                        calling_stmtnode = calling_stmtg.node_for_stmt[edge.call_stmt]
                        assert calling_stmtnode

                        entry_stmtnode = called_stmtg.entry
                        assert entry_stmtnode

                        exit_stmtnode = called_stmtg.exit
                        assert exit_stmtnode

                        superedge_call = self.add_edge(
                            calling_stmtg.supernode_for_stmtnode[calling_stmtnode][0],
                            called_stmtg.supernode_for_stmtnode[entry_stmtnode],
                            CallToStart,
                            None)
                        superedge_return = self.add_edge(
                            called_stmtg.supernode_for_stmtnode[exit_stmtnode],
                            calling_stmtg.supernode_for_stmtnode[calling_stmtnode][1],
                            ExitToReturnSite,
                            None)
                        superedge_return.calling_stmtnode = calling_stmtnode

    def _make_edge(self, srcnode, dstnode, cls, edge):
        return cls(srcnode, dstnode, edge)

    def get_entry_nodes(self):
        """
	/* At file scope, the presence of a `static' or `register' storage
	   class specifier, or the absence of all storage class specifiers
	   makes this declaration a definition (perhaps tentative).  Also,
	   the absence of `static' makes it public.  */
	if (current_scope == file_scope)
	  {
	    TREE_PUBLIC (decl) = storage_class != csc_static;
	    TREE_STATIC (decl) = !extern_ref;
	  }
          """
        # For now, assume all non-static functions are possible entrypoints:
        for fun in self.stmtg_for_fun:
            # Only for non-static functions:
            if fun.decl.is_public:
                stmtg = self.stmtg_for_fun[fun]
                yield stmtg.supernode_for_stmtnode[stmtg.entry]

class SupergraphNode(Node):
    """
    A node in the supergraph, wrapping a StmtNode
    """
    def __init__(self, innernode, stmtg):
        Node.__init__(self)
        self.innernode = innernode
        self.stmtg = stmtg

    def to_dot_label(self, ctxt):
        return self.innernode.to_dot_label(ctxt)

    def to_dot_html(self, ctxt):
        return self.innernode.to_dot_html(ctxt)

    def __str__(self):
        return str(self.innernode)

    def __repr__(self):
        return 'SupergraphNode(%r)' % self.innernode

    @property
    def stmt(self):
        return self.innernode.get_stmt()

    def get_stmt(self):
        return self.innernode.get_stmt()

    def get_gcc_loc(self):
        return self.innernode.get_gcc_loc()

    def get_subgraph(self, ctxt):
        return self.stmtg.fun.decl.name

    @property
    def function(self):
        """
        Get the gcc.Function for this node
        """
        return self.stmtg.fun

class CallNode(SupergraphNode):
    """
    A first node for a gcc.GimpleCall, representing the invocation of the
    function.
    It has the same stmt (the gcc.GimpleCall) as the ReturnNode
    """
    pass

class ReturnNode(SupergraphNode):
    """
    A second node for a gcc.GimpleCall, representing the assignment of the
    return value from the completed call into the LHS.
    It has the same stmt (the gcc.GimpleCall) as the CallNode
    """
    pass

class SupergraphEdge(Edge):
    """
    An edge in the supergraph, wrapping a StmtEdge,
    or None for the intraprocedual edges for function call/return
    """
    def __init__(self, srcnode, dstnode, inneredge):
        Edge.__init__(self, srcnode, dstnode)
        self.inneredge = inneredge

    def to_dot_label(self, ctxt):
        return self.inneredge.to_dot_label(ctxt)

    @property
    def true_value(self):
        return self.inneredge.true_value

    @property
    def false_value(self):
        return self.inneredge.false_value

class CallToReturnSiteEdge(SupergraphEdge):
    """
    The intraprocedural edge for a function call, from
    the gcc.GimpleCall to the next statement
    """
    def to_dot_label(self, ctxt):
        return 'within function'

    def to_dot_attrs(self, ctxt):
        return ' penwidth=2'

class CallToStart(SupergraphEdge):
    """
    The interprocedural edge for the start of a function call: from
    the gcc.GimpleCall to the entry node of the callee
    """
    def to_dot_label(self, ctxt):
        return 'call of %s' % self.dstnode.function.decl.name

    def to_dot_attrs(self, ctxt):
        #return ' constraint=false, style=dotted'
        return ' style=dotted'

class ExitToReturnSite(SupergraphEdge):
    """
    The interprocedural edge for the end of a function call: from
    the exit node of the callee to the successor node of the
    gcc.GimpleCall within the caller
    """
    def to_dot_label(self, ctxt):
        return 'return to %s' % self.dstnode.function.decl.name

    def to_dot_attrs(self, ctxt):
        #return ' constraint=false, style=dotted'
        return ' style=dotted'
