from __future__ import absolute_import, division, print_function, unicode_literals

from six import string_types

from ..errors import KaffeError, print_stderr
from ..graph import GraphBuilder, NodeMapper
from ..layers import NodeKind
from ..transformers import (DataInjector, DataReshaper, NodeRenamer, ReLUFuser,
                            BatchNormScaleBiasFuser, BatchNormPreprocessor, ParameterNamer)


class TensorFlowNode(object):
    """An intermediate representation for TensorFlow operations."""
    def __init__(self, op, *args, **kwargs):
        # A string corresponding to the TensorFlow operation
        self.op = op
        # Positional arguments for the operation
        self.args = args
        # Keyword arguments for the operation
        self.kwargs = list(kwargs.items())
        # The source Caffe node
        self.node = None

    def format(self, arg):
        """Returns a string representation for the given value."""
        return "'%s'" % arg if isinstance(arg, string_types) else str(arg)

    def pair(self, key, value):
        """Returns key=formatted(value)."""
        return '%s=%s' % (key, self.format(value))

    def emit(self):
        """Emits the Python source for this node."""
        # Format positional arguments
        args = [self.format(arg) for arg in self.args]
        # Format any keyword arguments
        if self.kwargs:
            args += [self.pair(k, v) for k, v in self.kwargs]
        # Set the node name
        args.append(self.pair('name', self.node.name))
        args = ', '.join(args)
        return '%s(%s)' % (self.op, args)


class MaybeActivated(object):
    def __init__(self, node, default=True):
        self.inject_kwargs = {}
        if node.metadata.get('relu', False) != default:
            self.inject_kwargs['relu'] = not default

    def __call__(self, *args, **kwargs):
        kwargs.update(self.inject_kwargs)
        return TensorFlowNode(*args, **kwargs)


class TensorFlowMapper(NodeMapper):
    def __init__(self, graph, use_padding_same=False):
        super(TensorFlowMapper, self).__init__(graph)
        self.use_padding_same = use_padding_same

    def get_kernel_params(self, node, use_padding_same=False):
        kernel_params = node.layer.kernel_parameters
        padding = {}
        if use_padding_same:
            padding['operator_padding'] = 'SAME'
        else:
            if kernel_params.pad_h:
                padding['pad_h'] = kernel_params.pad_h
            if kernel_params.pad_w:
                padding['pad_w'] = kernel_params.pad_w
        return kernel_params, padding

    def map_convolution(self, node):
        kernel_params, kwargs = self.get_kernel_params(node, use_padding_same=self.use_padding_same)
        h = kernel_params.kernel_h
        w = kernel_params.kernel_w
        c_o = node.output_shape[1]
        group = node.parameters.group
        if group != 1:
            kwargs['group'] = group
        if not node.parameters.bias_term:
            kwargs['biased'] = False
        assert kernel_params.kernel_h == h
        assert kernel_params.kernel_w == w
        return MaybeActivated(node)('conv', kernel_params.kernel_h, kernel_params.kernel_w, c_o,
                                    kernel_params.stride_h, kernel_params.stride_w, **kwargs)

    def map_relu(self, node):
        return TensorFlowNode('relu')

    def map_pooling(self, node):
        pool_type = node.parameters.pool
        if pool_type == 0:
            pool_op = 'max_pool'
        elif pool_type == 1:
            pool_op = 'avg_pool'
        else:
            # Stochastic pooling, for instance.
            raise KaffeError('Unsupported pooling type.')
        (kernel_params, padding) = self.get_kernel_params(node)
        return TensorFlowNode(pool_op, kernel_params.kernel_h, kernel_params.kernel_w,
                              kernel_params.stride_h, kernel_params.stride_w, **padding)

    def map_inner_product(self, node):
        # TODO: Axis
        assert node.parameters.axis == 1
        # TODO: Unbiased
        assert node.parameters.bias_term
        return MaybeActivated(node)('fc', node.parameters.num_output)

    def map_softmax(self, node):
        return TensorFlowNode('softmax')

    def map_lrn(self, node):
        params = node.parameters
        # The window size must be an odd value. For a window
        # size of (2*n+1), TensorFlow defines depth_radius = n.
        assert params.local_size % 2 == 1
        # Caffe scales by (alpha/(2*n+1)), whereas TensorFlow
        # just scales by alpha (as does Krizhevsky's paper).
        # We'll account for that here.
        alpha = params.alpha / float(params.local_size)
        return TensorFlowNode('lrn', int(params.local_size / 2), alpha, params.beta)

    def map_concat(self, node):
        axis = (2, 3, 1, 0)[node.parameters.axis]
        return TensorFlowNode('concat', axis)

    def map_dropout(self, node):
        return TensorFlowNode('dropout', node.parameters.dropout_ratio)

    def map_batch_norm(self, node):
        scale_offset = len(node.data) == 4
        kwargs = {} if scale_offset else {'scale_offset': False}
        return MaybeActivated(node, default=False)('batch_normalization', **kwargs)

    def map_eltwise(self, node):
        operations = {0: 'multiply', 1: 'add', 2: 'max'}
        op_code = node.parameters.operation
        try:
            return TensorFlowNode(operations[op_code])
        except KeyError:
            raise KaffeError('Unknown elementwise operation: {}'.format(op_code))

    def commit(self, chains):
        return chains


class TensorFlowEmitter(object):
    def __init__(self, tab=None):
        self.tab = tab or ' ' * 4
        self.prefix = ''

    def indent(self):
        self.prefix += self.tab

    def outdent(self):
        self.prefix = self.prefix[:-len(self.tab)]

    def statement(self, s):
        return self.prefix + s + '\n'

    def emit_imports(self):
        return self.statement('from caffeflow.tensorflow import Network\n')

    def emit_class_def(self, name):
        return self.statement('class %s(Network):' % (name))

    def emit_setup_def(self):
        return self.statement('def setup(self):')

    def emit_parents(self, chain):
        assert len(chain)
        s = '(self.feed('
        sep = ', \n' + self.prefix + (' ' * len(s))
        s += sep.join(["'%s'" % parent.name for parent in chain[0].node.parents])
        return self.statement(s + ')')

    def emit_node(self, node):
        return self.statement(' ' * 5 + '.' + node.emit())

    def emit(self, name, chains):
        s = self.emit_imports()
        s += self.emit_class_def(name)
        self.indent()
        s += self.emit_setup_def()
        self.indent()
        blocks = []
        for chain in chains:
            b = ''
            b += self.emit_parents(chain)
            for node in chain:
                b += self.emit_node(node)
            blocks.append(b[:-1] + ')')
        s = s + '\n\n'.join(blocks)
        return s


class TensorFlowTransformer(object):
    def __init__(self, def_path, data_path, verbose=True, phase='test', use_padding_same=False):
        self.graph = None

        self.verbose = verbose
        self.phase = phase
        self.load(def_path, data_path, phase)
        self.params = None
        self.source = None
        self.use_padding_same = use_padding_same

    def load(self, def_path, data_path, phase):
        # Build the graph
        graph = GraphBuilder(def_path, phase).build()

        if data_path is not None:
            # Load and associate learned parameters
            graph = DataInjector(def_path, data_path)(graph)

        # Transform the graph
        transformers = [
            # Fuse split batch normalization layers
            BatchNormScaleBiasFuser(),

            # Fuse ReLUs
            # TODO: Move non-linearity application to layer wrapper, allowing
            # any arbitrary operation to be optionally activated.
            ReLUFuser(allowed_parent_types=[NodeKind.Convolution, NodeKind.InnerProduct,
                                            NodeKind.BatchNorm]),

            # Rename nodes
            # Slashes are used for scoping in TensorFlow. Replace slashes
            # in node names with underscores.
            # (Caffe's GoogLeNet implementation uses slashes)
            NodeRenamer(lambda node: node.name.replace('/', '_'))
        ]
        self.graph = graph.transformed(transformers)

        # Display the graph
        if self.verbose:
            print_stderr(self.graph)

    def transform_data(self):
        if self.params is None:
            transformers = [

                # Reshape the parameters to TensorFlow's ordering
                DataReshaper({
                    # (c_o, c_i, h, w) -> (h, w, c_i, c_o)
                    NodeKind.Convolution: (2, 3, 1, 0),

                    # (c_o, c_i) -> (c_i, c_o)
                    NodeKind.InnerProduct: (1, 0)
                }),

                # Pre-process batch normalization data
                BatchNormPreprocessor(),

                # Convert parameters to dictionaries
                ParameterNamer(),
            ]
            self.graph = self.graph.transformed(transformers)
            self.params = {node.name: node.data for node in self.graph.nodes if node.data}
        return self.params

    def transform_source(self):
        if self.source is None:
            mapper = TensorFlowMapper(self.graph, use_padding_same=self.use_padding_same)
            chains = mapper.map()
            emitter = TensorFlowEmitter()
            self.source = emitter.emit(self.graph.name, chains)
        return self.source
