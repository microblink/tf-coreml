from __future__ import print_function
from six import string_types as _string_types

import numpy as np
import tensorflow as tf
import coremltools
from tensorflow.python.util import compat
from coremltools.models.neural_network import NeuralNetworkBuilder
from coremltools.models import datatypes, utils, MLModel
from ._ops_to_layers import convert_ops_to_layers
from . import _ops_to_layers
from ._interpret_shapes import _interpret_shape as interpret_shape
from ._tf_graph_transform import _topological_sort_ops, _find_unused_ops
from .optimizations._optimize_nn_spec import optimize_nn_spec

class SupportedVersion():
    # Supported iOS Version
    # New OS Version must be added at the end to maintain backward version index
    supported_ios_version = ['11.2', '12', '13']
    IOS_13_VERSION = supported_ios_version.index('13')
    ND_ARRARY_SUPPORT = IOS_13_VERSION

    @staticmethod
    def ios_support_check(target_ios):
        return target_ios in SupportedVersion.supported_ios_version

    @staticmethod
    def is_nd_array_supported(target_ios):
        if not SupportedVersion.ios_support_check(target_ios):
            raise TypeError('{} not supported. Please provide one of target iOS: {}', target_ios, SupportedVersion.supported_ios_version)
        
        target_ios_index = SupportedVersion.supported_ios_version.index(target_ios)
        return SupportedVersion.ND_ARRARY_SUPPORT <= target_ios_index

    @staticmethod
    def get_supported_ios():
        return SupportedVersion.supported_ios_version

    @staticmethod
    def get_specification_version(target_ios):
        if not SupportedVersion.ios_support_check(target_ios):
            raise TypeError('{} not supported. Please provide one of target iOS: {}', target_ios, SupportedVersion.supported_ios_version)

        if target_ios == '11.2':
            return IOS_11_2_SPEC_VERSION
        elif target_ios == '12':
            return IOS_12_SPEC_VERSION
        else:
            return IOS_13_SPEC_VERSION            

# Checks input and output naming convention
# With `target_ios=13` i.e. new tf-coreml path drops ':' from the input and output
# names
def check_input_output_names(input_name_shape_dict, output_feature_names):
  new_input = []
  old_input = []
  new_output = []
  old_output = []
  for _key in input_name_shape_dict:
    if ':' in _key:
      new_input.append(_key.split(':')[0])
      old_input.append(_key)
      
  for _output in output_feature_names:
    if ':' in _output:
      new_output.append(_output.split(':')[0])
      old_output.append(_output)
      
  if len(new_input) > 0 or len(new_output) > 0:
    input_string = ''
    output_string = ''
    if len(new_input) > 0:
      input_string = 'Input: ' + str(new_input) + ' instead of ' + str(old_input) + '\n'
    if len(new_output) > 0:
      output_string = 'Output: ' + str(new_output) + ' instead of ' + str(old_output)

    raise ValueError('with target deployment > \'12\', the converter drops \':\' convention for input and output.'
                     ' Please provide input and output without \':\' e.g. `input` instead of `input:0`\n'
                     'Recommendation: \n {} {}'.format(input_string, output_string))

# Context stores useful information about TF graph and the conversion process
class Context(object):
  def __init__(self, consts, shape_dict, ops, blob_graph, output_features):
    self.builder = None
    self.consts = consts
    self.shape_dict = shape_dict #Tensor name --> shape ({str: list})
    self.translated = {x: True for x in self.consts.keys()}
    self.out_name_to_in_name = {} #for blobs which come from a no-op
    self.all_ops = ops
    self.output_names = []
    for out in output_features:
      self.output_names.append(out[0])

    self.skip_map_names = {}
    # Set of all load constants added to the CoreML graph
    self.load_constants_mlmodel = {}

    # Tensor name to list of ops it feeds into
    self.blob_graph = blob_graph
    # Tensor name sto and their inferred rank 4 shape (Batch/Sequennce, C, H, W)
    self.shape_dict_rank_4 = {}
    # Tensor name to labeled shapes (one of 'S','C','H','W').
    # e.g.: 'input' tensor which has shape (1,224,224,3) --> ('S','H','W','C')
    self.dim_labels = {}
    # Whether to use DFS search to infer shapes on the path to conv layers
    self.use_dfs_shape_infer = True #True
    self.session = None
    self.input_feed_dict = None
    self.unused_ops = [] # list of op names that can be skipped for conversion as they do not connect to the output
    self.effectively_constant_ops = [] # list of ops that are not of type "Const", but their output does not change with differently valued graph input
    self.skip_ops = []
    self.add_custom_layers = False
    self.custom_conversion_functions = {}
    self.ops_converted_to_custom_layers = [] # list of ops that have been converted to custom coreml layers

def _infer_coreml_input_shape(tf_shape):
  """Infer CoreML input shape from TensorFlow shape.
  """
  if len(tf_shape) == 0:
    shape = [1, 1, 1]
  elif len(tf_shape) == 1:
    # TODO - remove style transfer 1D hack
    # Input is 1D but it goes to the width dimension: (1,1,W)
    shape = [1, 1, tf_shape[0]]  #(C,H,W)
  elif len(tf_shape) == 2:
    # assume (Batch, Channels) - Batch dimension should be dropped
    shape = [tf_shape[1]]
  elif len(tf_shape) == 3:
    # assume (Batch, Sequence-Length, channels)
    shape = [tf_shape[2], 1, tf_shape[1]]
  elif len(tf_shape) == 4:   #(B,H,W,C) --> (C,H,W)
    shape = [tf_shape[3], tf_shape[1], tf_shape[2]] #(C,H,W)
  else:
    raise ValueError('Unrecognized TensorFlow input shape' + str(tf_shape))
  return shape

def _infer_coreml_output_shape(tf_shape):
  """Infer CoreML output shape from TensorFlow shape.
  """
  shape = []
  if len(tf_shape) == 1:
    shape = [tf_shape[0], 1, 1]
  elif len(tf_shape) == 2:
    if tf_shape[0] == 1:
      # (B,C)
      shape = [tf_shape[1]]
    else:
      shape = None
  elif len(tf_shape) == 3:
    # since output shape is not required by CoreML and rank-3 tensor in TF is ambiguous, we do not assign a shape
    shape = None
  elif len(tf_shape) == 4:
    shape = [tf_shape[3], tf_shape[1], tf_shape[2]] #(C,H,W)
  elif len(tf_shape) == 0: # scalar
    shape = [1]
  else:
    raise ValueError('Unrecognized TensorFlow output shape ' + str(tf_shape))
  return shape

def _check_unsupported_ops(ops, output_feature_names, skip_ops):
  '''
  Checks all the ops till the desired outputs are reached.
  From these ops it collects all the ops that are unsupported.
  Error out if there is at least one unsupported op.
  :param ops: ops of the TF graph
  :param output_feature_names: [str]: list of output names 
  :param skip_ops: [str]: list of op names that can be skipped since they either do not depend on the 
  actual value of the input or do not connect to the final output
  '''
  unsupported_op_types = []
  outputs_encountered = {}
  for op in ops:
    all_outputs_reached = True
    for out in output_feature_names:
      if out not in outputs_encountered:
        all_outputs_reached = False
        break
    if all_outputs_reached:
      break
    if op.type not in _ops_to_layers._OP_REGISTRY and \
       op.type not in unsupported_op_types and \
       op.name not in skip_ops:
      unsupported_op_types.append(op.type)
    for out in op.outputs:
      outputs_encountered[out.name] = True
  if len(unsupported_op_types) > 0:
      raise NotImplementedError("Unsupported Ops of type: %s" % (
        ','.join(unsupported_op_types)))

def _convert_pb_to_mlmodel(tf_model_path,
                           mlmodel_path,
                           output_feature_names,
                           input_name_shape_dict={},
                           image_input_names=None,
                           is_bgr=False,
                           red_bias=0.0,
                           green_bias=0.0,
                           blue_bias=0.0,
                           gray_bias=0.0,
                           image_scale=1.0,
                           class_labels=None,
                           predicted_feature_name=None,
                           predicted_probabilities_output='',
                           add_custom_layers=False,  # type: bool
                           custom_conversion_functions={}  # type: Dict[Text, Any]
                           ):

  # Load the TF graph
  print('')
  print('Loading the TF graph...')
  with open(tf_model_path, 'rb') as f:
    serialized = f.read()

  tf.reset_default_graph()
  gdef = tf.GraphDef()
  gdef.ParseFromString(serialized)

  with tf.Graph().as_default() as g:
    tf.import_graph_def(gdef, name='')

  sess = tf.Session(graph=g)
  OPS = g.get_operations()

  if 'DecodeJpeg' in [op.type for op in OPS]:
    raise NotImplementedError("Unsupported Op of type: DecodeJpeg. "
                              "Kindly refer to the \"examples/inception_v3.ipynb\" notebook, "
                              "on the tfcoreml github page, to see how to strip input "
                              "pre-processing from the TF graph before conversion to CoreML.")


  print('Graph Loaded.')
  # Sort the ops in topological order and check whether the graph has cycles, if yes, error out
  OPS = _topological_sort_ops(OPS)

  SHAPE_DICT = {} #Tensor name --> shape ({str: list})
  CONSTS = {} #Const Tensor name --> value
  BLOB_GRAPH = {} #Blob name to list of ops it feeds into

  # Make Dictionary of Input blob to the list of ops it feeds into
  for op in OPS:
    for inp in op.inputs:
      if inp.name in BLOB_GRAPH:
        BLOB_GRAPH[inp.name].append(op)
    for out in op.outputs:
      if out.name not in BLOB_GRAPH:
        BLOB_GRAPH[out.name] = []

  # Fill in input information
  input_features = []
  output_features = []
  input_feed_dict = dict() #Input tensors' values
  input_feed_dict2 = dict() # used later to find skippable ops

  # run through all placeholders
  for op in OPS:
    output_names = set([compat.as_str_any(x.name) for x in op.outputs])
    if op.type == 'Placeholder':
      # Handle placeholders -- all placeholders are inputs
      assert not any(filter(output_names.__contains__, output_feature_names)), \
          ('Output feature cannot be a placeholder')
      input_name = compat.as_str_any(op.outputs[0].name)
      shape = op.outputs[0].get_shape()

      if input_name in input_name_shape_dict:
        shape = input_name_shape_dict[input_name]
      elif shape.is_fully_defined():
        shape = shape.as_list()
      else:
        try:
          shape_list = shape.as_list()
        except:
          raise ValueError('Please provide the shape for the input {} through the argument \'input_name_shape_dict\''.format(input_name))
        if shape_list[0] is None and None not in shape_list[1:]:
          shape = [1] + shape_list[1:]
        else:
          raise ValueError("%s is a placeholder with incomplete shape %s. Please provide the 'input_name_shape_dict' "
                       "argument to the convert function, with the fully defined shape." %(input_name, str(shape)))

      if len(shape) == 0: # scalar - use a 1
        input_feed_dict[op.outputs[0]] = 1
        input_feed_dict2[op.outputs[0]] = 1
      else:
        input_feed_dict[op.outputs[0]] = np.random.rand(*shape)
        input_feed_dict2[op.outputs[0]] = 255*np.random.rand(*shape)

      SHAPE_DICT[input_name] = list(shape)

  # Find "effectively_constant_ops": ops whose output(s) do not change with different valued Graph level inputs
  # Find "unused_ops" : ops that are not connected to the output(s)
  unused_ops = []
  effectively_constant_ops = []
  try:
    print("Now finding ops in the TF graph that can be dropped for inference")
    unused_ops, effectively_constant_ops = _find_unused_ops(OPS, sess, output_feature_names, input_feed_dict, input_feed_dict2) # return type: List[str], List[str]
  except:
    pass

  # Populate SHAPE_DICT: Dictionary for all tensor blobs in the graph and their shapes
  shapes_wanted = [] # list of output names
  consts_wanted = []
  for op in OPS:
    for out in op.outputs:
      shape = out.get_shape()
      if not shape.is_fully_defined():
        shapes_wanted.append((compat.as_str_any(out.name), out))
      else:
        SHAPE_DICT[compat.as_str_any(out.name)] = shape.as_list()

    is_const = False
    if op.type == 'Const':
      is_const = True

    if op.type == 'Dequantize' and op.name in effectively_constant_ops:
      is_const = True

    if is_const:
      const = op.outputs[0]
      consts_wanted.append((compat.as_str_any(const.name), const))

  print('Collecting all the \'Const\' ops from the graph, by running it....')
  if len(shapes_wanted) > 0 or len(consts_wanted) > 0:
    tensor_names, tensors = zip(*(shapes_wanted+consts_wanted))
    if len(consts_wanted) > 0:
      const_tensor_names, _ = zip(*consts_wanted)
    else:
      const_tensor_names = []
    tensors_evaluated = sess.run(tensors, feed_dict=input_feed_dict)
    for i in range(len(tensor_names)):
      if tensor_names[i] not in SHAPE_DICT:
        SHAPE_DICT[tensor_names[i]] = list(tensors_evaluated[i].shape)
      if tensor_names[i] in const_tensor_names and tensor_names[i] not in CONSTS:
        CONSTS[tensor_names[i]] = tensors_evaluated[i]
  print('Done.')

  # Fill in output information
  for op in OPS:
    output_names = set([compat.as_str_any(x.name) for x in op.outputs])
    if any(filter(output_names.__contains__, output_feature_names)):
      # retrieve model outputs
      for output in [x for x in op.outputs if x.name in output_feature_names]:
        #infer shape for Core ML
        tf_shape = SHAPE_DICT[compat.as_str_any(output.name)]
        shape = _infer_coreml_output_shape(tf_shape)
        out_name = output.name
        if shape is None:
          output_features.append(
            (compat.as_str_any(out_name), None))
        else:
          output_features.append(
            (compat.as_str_any(out_name), datatypes.Array(*shape)))

  if len(output_features) != len(output_feature_names):
    all_out_names_in_graph = [out_[0] for out_ in output_features]
    for given_out_name in output_feature_names:
      if given_out_name not in all_out_names_in_graph:
        raise ValueError("output name: {}, was provided, but the Tensorflow graph does not contain a tensor with this name.".format(given_out_name))

  if not add_custom_layers:
    _check_unsupported_ops(OPS, output_feature_names, effectively_constant_ops + unused_ops)
  print('Now starting translation to CoreML graph.')


  # Load all the dictionaries in the object of the class "context"
  context = Context(CONSTS, SHAPE_DICT, OPS, BLOB_GRAPH, output_features)

  # Interpret Input shapes and fill in input information for Core ML
  # (now that SHAPE_DICT and CONSTS are complete)
  sequence_inputs = dict()
  for input_tensor in input_feed_dict:
    input_name = compat.as_str_any(input_tensor.name)
    shape = SHAPE_DICT[input_name]

    if context.use_dfs_shape_infer:
      status = interpret_shape(input_name, context)
    else:
      status = False
    if status:
      print('Automatic shape interpretation succeeded for input blob %s' \
          %(input_name))
      shape = context.shape_dict_rank_4[input_name]

    if len(shape) == 4 and shape[0] != 1:
      sequence_inputs[input_name] = shape[0]

    # if the consumer of input_tensor is an one-hot encoding op,
    # treat it as a sequence.
    consumer_op = input_tensor.consumers()[0]
    if consumer_op.type == 'OneHot':
      shape = [1,]
      sequence_inputs[input_name] = -1
    else:
      shape = _infer_coreml_input_shape(shape)
    input_features.append(
        (compat.as_str_any(input_name), datatypes.Array(*shape)))

  # Set classifier flag
  is_classifier = class_labels is not None
  mode = 'classifier' if is_classifier else None

  # Convert the TF graph with builder
  input_features = list(input_features)
  output_features = list(output_features)
  builder = NeuralNetworkBuilder(input_features, output_features, mode=mode)
  context.builder = builder
  context.session = sess
  context.input_feed_dict = input_feed_dict
  context.unused_ops = unused_ops
  context.effectively_constant_ops = effectively_constant_ops
  context.add_custom_layers = add_custom_layers
  context.custom_conversion_functions = custom_conversion_functions
  convert_ops_to_layers(context)
  sess.close()

  #optimizations on the nn spec
  optimize_nn_spec(spec=builder.spec)

  #Add a description for inputs that are sequences
  for i, inputs in enumerate(builder.spec.description.input):
    if inputs.name in sequence_inputs:
      seq_length = sequence_inputs[inputs.name]
      proto_shape = []
      if inputs.type.HasField('multiArrayType'):
        proto_shape = [int(s) for s in inputs.type.multiArrayType.shape]
      if seq_length == -1:
          msg = 'This input is a sequence'
          if len(proto_shape):
            msg += '. Feed it an MLMultiArray of shape {} at runtime'.format(str(['Seq_size', '1'] + proto_shape))
      else:
          msg = 'This input is a sequence of length ' + str(seq_length)
          if len(proto_shape):
            msg += '. Feed it an MLMultiArray of shape {} at runtime'.format(str([seq_length, 1] + proto_shape))
      builder.spec.description.input[i].shortDescription = msg

  # Add image input identifier
  if image_input_names is not None and isinstance(
      image_input_names, _string_types):
    image_input_names = [image_input_names]

  # Replace all input/output blob names with ":" to "__" for compatible
  # auto-generated Objective C / Swift code
  interface_blob_names = []
  for idx, in_blob in enumerate(builder.spec.description.input):
    interface_blob_names.append(in_blob.name)
    builder.spec.description.input[idx].name = in_blob.name.replace(':', '__').replace('/', '__')
  for idx, out_blob in enumerate(builder.spec.description.output):
    interface_blob_names.append(out_blob.name)
    builder.spec.description.output[idx].name = out_blob.name.replace(':', '__').replace('/', '__')

  nn_spec = builder.nn_spec
  for i, spec_layer in enumerate(nn_spec.layers):
    for j, blob in enumerate(spec_layer.input):
      name = spec_layer.input[j]
      if name in interface_blob_names:
        spec_layer.input[j] = name.replace(':', '__').replace('/', '__')
    for j, blob in enumerate(spec_layer.output):
      name = spec_layer.output[j]
      if name in interface_blob_names:
        spec_layer.output[j] = name.replace(':', '__').replace('/', '__')

  if image_input_names is not None:
    for i, img in enumerate(image_input_names):
      image_input_names[i] = img.replace(':', '__').replace('/', '__')

  # Add classifier classes (if applicable)
  if is_classifier:
    classes_in = class_labels
    if isinstance(classes_in, _string_types):
      import os
      if not os.path.isfile(classes_in):
        raise ValueError("Path to class labels (%s) does not exist." % \
            classes_in)
      with open(classes_in, 'r') as f:
        classes = f.read()
      classes = classes.splitlines()
    elif type(classes_in) is list: # list[int or str]
      classes = classes_in
    else:
      raise ValueError('Class labels must be a list of integers / strings,'\
          ' or a file path')

    if predicted_feature_name is not None:
      builder.set_class_labels(
          classes, predicted_feature_name=predicted_feature_name,
          prediction_blob=predicted_probabilities_output)
    else:
      builder.set_class_labels(classes)

  # Set pre-processing parameters
  builder.set_pre_processing_parameters(image_input_names=image_input_names,
                                        is_bgr=is_bgr,
                                        red_bias=red_bias,
                                        green_bias=green_bias,
                                        blue_bias=blue_bias,
                                        gray_bias=gray_bias,
                                        image_scale=image_scale)

  print("Translation to CoreML spec completed. Now compiling and saving the CoreML model.")
  try:
    import coremltools
    if mlmodel_path is not None:
      coremltools.models.utils.save_spec(builder.spec, mlmodel_path)
      print("\n Core ML model generated. Saved at location: %s \n" % (mlmodel_path))
    mlmodel = MLModel(builder.spec)
  except RuntimeError as e:
    raise ValueError('Compilation failed: {}'.format(str(e)))

  print('Core ML input(s): \n', builder.spec.description.input)
  print('Core ML output(s): \n', builder.spec.description.output)

  # print information about all ops for which custom layers have been added
  if len(context.ops_converted_to_custom_layers) > 0:
    print('\n')
    print("Custom layers have been added to the CoreML model "
          "corresponding to the following ops in the TF graph: ")
    for i, op in enumerate(context.ops_converted_to_custom_layers):
      input_info = []
      for input_ in op.inputs:
        input_info.append((str(input_.name), context.shape_dict.get(input_.name, str("Shape not available"))))
      output_info = []
      for output_ in op.outputs:
        output_info.append((str(output_.name), context.shape_dict.get(output_.name, str("Shape not available"))))
      print("{}/{}: op type: {}, op input names and shapes: {}, op output names and shapes: {}".
            format(i + 1, len(context.ops_converted_to_custom_layers), op.type, str(input_info), str(output_info)))

  # Return the protobuf model
  return mlmodel


def convert(tf_model_path,
            mlmodel_path=None,
            output_feature_names=None,
            input_name_shape_dict=None,
            image_input_names=None,
            is_bgr=False,
            red_bias=0.0,
            green_bias=0.0,
            blue_bias=0.0,
            gray_bias=0.0,
            image_scale=1.0,
            class_labels=None,
            predicted_feature_name=None,
            predicted_probabilities_output='',
            add_custom_layers=False,  # type: bool
            custom_conversion_functions={},  # type: Dict[Text, Any]
            custom_shape_functions={}, # type: Dict[Text, Any]
            target_ios='12',
            ):

  """
  Convert a frozen TensorFlow grpah (.pb format) to the CoreML format (.mlmodel)

  Parameters
  ----------
  tf_model_path : str
      Path to the frozen .pb model

  mlmodel_path: str
      Path to where the generated .mlmodel will be stored

  output_feature_names: [str]
      List of strings. Names of the output tensors.

  input_name_shape_dict: {str: [int]}
      Dictionary of input tensor names and their corresponding shapes expressed
      as a list of ints

  image_input_names: [str] | str
      Input names (a subset of the keys of input_name_shape_dict)
      that can be treated as images by Core ML. All other inputs
      are treated as MultiArrays.

  is_bgr: bool | dict():
      Applicable only if image_input_names is specified.
      To specify different values for each image input provide a dictionary with input names as keys.    

  red_bias: float | dict()
      Bias value to be added to the red channel of the input image, after applying scale.
      Defaults to 0.0
      Applicable only if image_input_names is specified.
      To specify different values for each image input provide a dictionary with input names as keys.    

  blue_bias: float | dict()
      Bias value to be added to the blue channel of the input image, after applying scale.
      Defaults to 0.0
      Applicable only if image_input_names is specified.
      To specify different values for each image input provide a dictionary with input names as keys.    

  green_bias: float | dict()
      Bias value to be added to the green channel of the input image, after applying scale.
      Defaults to 0.0
      Applicable only if image_input_names is specified.
      To specify different values for each image input provide a dictionary with input names as keys.    

  gray_bias: float | dict()
      Bias value to be added to the input image (in grayscale), after applying scale.
      Defaults to 0.0
      Applicable only if image_input_names is specified.
      To specify different values for each image input provide a dictionary with input names as keys.    

  image_scale: float | dict()
      Value by which input images will be scaled before bias is added and 
      Core ML model makes a prediction. Defaults to 1.0.
      Applicable only if image_input_names is specified.
      To specify different values for each image input provide a dictionary with input names as keys.     
      
  class_labels: list[int or str] | str
      Class labels (applies to classifiers only) that map the index of the
      output of a neural network to labels in a classifier.
      If the provided class_labels is a string, it is assumed to be a
      filepath where classes are parsed as a list of newline separated
      strings.

  predicted_feature_name: str
      Name of the output feature for the class labels exposed in the Core ML
      model (applies to classifiers only). Defaults to 'classLabel'
        
  predicted_probabilities_output: str
      Name of the neural network output to be interpreted as the predicted
      probabilities of the resulting classes. Typically the output of a
      softmax function.   
      
  add_custom_layers: bool
      Flag to turn on addition of custom CoreML layers for unsupported TF ops or attributes within
      a supported op.
  
  custom_conversion_functions: dict(): {Text: func(**kwargs)}
      Argument to provide user-defined functions for converting Tensorflow operations (op, for short).
      A dictionary with keys corresponding to the names or types of the TF ops and values as handle to user-defined functions.  
      The keys can be either the type of the op or the name of the op. If former, then the function is called whenever the op
      of that type is encountered during conversion. By using op names, specific ops can be targeted which is 
      useful for handling unsupported configuration in an op.
      The function receives multiple arguments: TF operation, the CoreML Neural network builder object, 
      dictionary containing the op's inputs that are constants and their values (as numpy arrays).
      The function can add custom layers or any other combination of CoreML layers to translate the TF op. 
      See "examples/custom_layer_examples.ipynb" jupyter-notebook for examples on using this argument. 
    
  custom_shape_functions: dict(): {Text: func()}
      Argument to provide user-defined functions to compute shape for given op.
      A dictionary with keys corresponding to the type of TF Op and value as hadnled to user-defined function.
      Function receives `layer specification` and `input shape` as a input.
      output of the function must be output shape for give op. (generally List).
      Custom shape function is required for adding custom layer in Core ML 3.
      If target_ios less than iOS 13 ('13'), then this option is ignored

  target_ios: str
      Target Deployment iOS Version (default: '12')
      Supported iOS version options: '11.2', '12', '13'        
      CoreML model produced by the converter will be compatible with the iOS version specified in this argument.
      e.g. if target_ios = '12', the converter would only utilize CoreML features released till iOS12 (equivalently macOS 10.14, watchOS 5 etc).

      iOS 11.2 (CoreML 0.8) does not support resize_bilinear, crop_resize layers 
        - (Supported features: https://github.com/apple/coremltools/releases/tag/v0.8)
      iOS 12 (CoreML 2.0)
        - (Supported features: https://github.com/apple/coremltools/releases/tag/v2.0)
      iSO 13 (CoreML 3.0)
        - (Supported features: https://github.com/apple/coremltools/releases/tag/3.0)
    
  Returns
  -------
  model: MLModel
      Model in Core ML format.

  """

  if not SupportedVersion.ios_support_check(target_ios):
    raise TypeError('{} not supported. Please provide one of target iOS: {}', target_ios, SupportedVersion.get_supported_ios())
     
  if SupportedVersion.is_nd_array_supported(target_ios):
    # Check input and output name for correct convention being used
    check_input_output_names(input_name_shape_dict, output_feature_names)
    
    mlmodel = coremltools.converters.tensorflow.convert(
                  tf_model_path,
                  inputs=input_name_shape_dict,
                  outputs=output_feature_names,
                  image_input_names=image_input_names,
                  is_bgr=is_bgr,
                  red_bias=red_bias,
                  green_bias=green_bias,
                  blue_bias=blue_bias,
                  gray_bias=gray_bias,
                  image_scale=image_scale,
                  class_labels=class_labels,
                  predicted_feature_name=predicted_feature_name,
                  predicted_probabilities_output=predicted_probabilities_output,
                  add_custom_layers=add_custom_layers,
                  custom_conversion_functions=custom_conversion_functions,
                  custom_shape_functions=custom_shape_functions)
    if mlmodel_path is not None:
      mlmodel.save(mlmodel_path)
    return mlmodel

  if input_name_shape_dict is None:
    input_name_shape_dict = {}

  if output_feature_names is None:
    raise ValueError('Output feature names must be provided.')

  return _convert_pb_to_mlmodel(
      tf_model_path,
      mlmodel_path,
      output_feature_names,
      input_name_shape_dict,
      image_input_names=image_input_names,
      is_bgr=is_bgr,
      red_bias=red_bias,
      green_bias=green_bias,
      blue_bias=blue_bias,
      gray_bias=gray_bias,
      image_scale=image_scale,
      class_labels=class_labels,
      predicted_feature_name=predicted_feature_name,
      predicted_probabilities_output=predicted_probabilities_output,
      add_custom_layers=add_custom_layers,
      custom_conversion_functions=custom_conversion_functions)
