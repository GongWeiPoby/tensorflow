# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
# pylint: disable=protected-access
"""Code for model cloning, plus model-related API entries.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tensorflow.python.keras import backend as K
from tensorflow.python.keras import metrics as metrics_module
from tensorflow.python.keras import optimizers
from tensorflow.python.keras.engine import sequential
from tensorflow.python.keras.engine import training
from tensorflow.python.keras.engine.base_layer import Layer
from tensorflow.python.keras.engine.input_layer import Input
from tensorflow.python.keras.engine.input_layer import InputLayer
from tensorflow.python.keras.engine.network import Network
from tensorflow.python.keras.saving import hdf5_format
from tensorflow.python.keras.saving import model_config
from tensorflow.python.keras.utils import generic_utils
from tensorflow.python.keras.utils.generic_utils import CustomObjectScope
from tensorflow.python.util import nest
from tensorflow.python.util.tf_export import keras_export


# API entries importable from `keras.models`:
Model = training.Model  # pylint: disable=invalid-name
Sequential = sequential.Sequential  # pylint: disable=invalid-name
save_model = hdf5_format.save_model
load_model = hdf5_format.load_model
model_from_config = model_config.model_from_config
model_from_yaml = model_config.model_from_yaml
model_from_json = model_config.model_from_json

# Callable used to clone a layer with weights preserved.
def share_weights(layer):
  return layer


def _clone_layer(layer):
  return layer.__class__.from_config(layer.get_config())


def _clone_functional_model(model, input_tensors=None, layer_fn=_clone_layer):
  """Clone a functional `Model` instance.

  Model cloning is similar to calling a model on new inputs,
  except that it creates new layers (and thus new weights) instead
  of sharing the weights of the existing layers.

  Input layers are always cloned.

  Arguments:
      model: Instance of `Model`.
      input_tensors: optional list of input tensors
          to build the model upon. If not provided,
          placeholders will be created.
      layer_fn: callable to be applied on non-input layers in the model. By
          default it clones the layer. Another example is to preserve the layer
          to share the weights. This is required when we create a per-replica
          copy of the model with distribution strategy; we want the weights to
          be shared but still feed inputs separately so we create new input
          layers.

  Returns:
      An instance of `Model` reproducing the behavior
      of the original model, on top of new inputs tensors,
      using newly instantiated weights.

  Raises:
      ValueError: in case of invalid `model` argument value or `layer_fn`
      argument value.
  """
  if not isinstance(model, Model):
    raise ValueError('Expected `model` argument '
                     'to be a `Model` instance, got ', model)
  if isinstance(model, Sequential):
    raise ValueError('Expected `model` argument '
                     'to be a functional `Model` instance, '
                     'got a `Sequential` instance instead:', model)
  if not model._is_graph_network:
    raise ValueError('Expected `model` argument '
                     'to be a functional `Model` instance, '
                     'but got a subclass model instead.')

  layer_map = {}  # Cache for created layers.
  tensor_map = {}  # Map {reference_tensor: corresponding_tensor}
  if input_tensors is None:
    # Create placeholders to build the model on top of.
    input_tensors = []
    for layer in model._input_layers:
      input_tensor = Input(
          batch_shape=layer._batch_input_shape,
          dtype=layer.dtype,
          sparse=layer.sparse,
          name=layer.name)
      input_tensors.append(input_tensor)
      # Cache newly created input layer.
      newly_created_input_layer = input_tensor._keras_history[0]
      layer_map[layer] = newly_created_input_layer
  else:
    # Make sure that all input tensors come from a Keras layer.
    # If tensor comes from an input layer: cache the input layer.
    input_tensors = nest.flatten(input_tensors)
    input_tensors_ = []
    for i in range(len(input_tensors)):
      input_tensor = input_tensors[i]
      if not K.is_keras_tensor(input_tensor):
        original_input_layer = model._input_layers[i]
        name = original_input_layer.name
        input_tensor = Input(tensor=input_tensor,
                             name='input_wrapper_for_' + name)

        input_tensors_.append(input_tensor)
        # Cache newly created input layer.
        newly_created_input_layer = input_tensor._keras_history[0]
        layer_map[original_input_layer] = newly_created_input_layer
      else:
        input_tensors_.append(input_tensor)
    input_tensors = input_tensors_

  for x, y in zip(model.inputs, input_tensors):
    tensor_map[x] = y

  if not callable(layer_fn):
    raise ValueError('Expected `layer_fn` argument to be a callable.')

  new_nodes = set()

  # Iterated over every node in the reference model, in depth order.
  depth_keys = list(model._nodes_by_depth.keys())
  depth_keys.sort(reverse=True)
  for depth in depth_keys:
    nodes = model._nodes_by_depth[depth]
    for node in nodes:
      # Recover the corresponding layer.
      layer = node.outbound_layer

      # Get or create layer.
      if layer not in layer_map:
        new_layer = layer_fn(layer)
        layer_map[layer] = new_layer
        layer = new_layer
      else:
        # Reuse previously cloned layer.
        layer = layer_map[layer]
        # Don't call InputLayer multiple times.
        if isinstance(layer, InputLayer):
          continue

      # If all previous input tensors are available in tensor_map,
      # then call node.inbound_layer on them.
      if all(
          tensor in tensor_map for tensor in nest.flatten(node.input_tensors)):
        computed_tensors = nest.map_structure(lambda t: tensor_map[t],
                                              node.input_tensors)
        # Call layer.
        kwargs = node.arguments or {}
        output_tensors = layer(computed_tensors, **kwargs)

        # Thread-safe way to keep track of what node was created.
        first_output_tensor = nest.flatten(output_tensors)[0]
        new_nodes.add(
            layer._inbound_nodes[first_output_tensor._keras_history[1]])

        for x, y in zip(
            nest.flatten(node.output_tensors), nest.flatten(output_tensors)):
          tensor_map[x] = y

  # Check that we did compute the model outputs,
  # then instantiate a new model from inputs and outputs.
  output_tensors = []
  for x in model.outputs:
    assert x in tensor_map, 'Could not compute output ' + str(x)
    output_tensors.append(tensor_map[x])

  input_tensors = nest.pack_sequence_as(model._nested_inputs, input_tensors)
  output_tensors = nest.pack_sequence_as(model._nested_outputs, output_tensors)
  model = Model(input_tensors, output_tensors, name=model.name)
  # Layers not directly tied to outputs of the Model, such as loss layers
  # created in `add_loss`.
  ancillary_layers = [
      layer for layer in layer_map.values() if layer not in model.layers
  ]
  if ancillary_layers:
    nodes = set(
        nest.flatten([layer._inbound_nodes for layer in ancillary_layers]))
    relevant_nodes = list(nodes.intersection(new_nodes))
    model._insert_layers(ancillary_layers, relevant_nodes=relevant_nodes)
  return model


def _clone_sequential_model(model, input_tensors=None, layer_fn=_clone_layer):
  """Clone a `Sequential` model instance.

  Model cloning is similar to calling a model on new inputs,
  except that it creates new layers (and thus new weights) instead
  of sharing the weights of the existing layers.

  Arguments:
      model: Instance of `Sequential`.
      input_tensors: optional list of input tensors
          to build the model upon. If not provided,
          placeholders will be created.
      layer_fn: callable to be applied on non-input layers in the model. By
          default it clones the layer. Another example is to preserve the layer
          to share the weights. This is required when we create a per-replica
          copy of the model with distribution strategy; we want the weights to
          be shared but still feed inputs separately so we create new input
          layers.

  Returns:
      An instance of `Sequential` reproducing the behavior
      of the original model, on top of new inputs tensors,
      using newly instantiated weights.

  Raises:
      ValueError: in case of invalid `model` argument value or `layer_fn`
      argument value.
  """
  if not isinstance(model, Sequential):
    raise ValueError('Expected `model` argument '
                     'to be a `Sequential` model instance, '
                     'but got:', model)

  if not callable(layer_fn):
    raise ValueError('Expected `layer_fn` argument to be a callable.')

  # Use model._layers to ensure that all layers are cloned. The model's layers
  # property will exclude the initial InputLayer (if it exists) in the model,
  # resulting in a different Sequential model structure.
  if input_tensors is None:
    layers = []
    for layer in model._layers:
      if isinstance(layer, InputLayer):
        layers.append(_clone_layer(layer))
      else:
        layers.append(layer_fn(layer))
    return Sequential(layers=layers, name=model.name)
  else:
    # If input tensors are provided, the original model's InputLayer is
    # overwritten with a different InputLayer.
    layers = [
        layer_fn(layer)
        for layer in model._layers
        if not isinstance(layer, InputLayer)
    ]
    if len(generic_utils.to_list(input_tensors)) != 1:
      raise ValueError('To clone a `Sequential` model, we expect '
                       ' at most one tensor '
                       'as part of `input_tensors`.')

    if isinstance(input_tensors, tuple):
      input_tensors = list(input_tensors)
    x = generic_utils.to_list(input_tensors)[0]
    if K.is_keras_tensor(x):
      origin_layer = x._keras_history[0]
      if isinstance(origin_layer, InputLayer):
        return Sequential(layers=[origin_layer] + layers, name=model.name)
      else:
        raise ValueError('Cannot clone a `Sequential` model on top '
                         'of a tensor that comes from a Keras layer '
                         'other than an `InputLayer`. '
                         'Use the functional API instead.')
    input_tensor = Input(tensor=x, name='input_wrapper_for_' + str(x.name))
    input_layer = input_tensor._keras_history[0]
    return Sequential(layers=[input_layer] + layers, name=model.name)


@keras_export('keras.models.clone_model')
def clone_model(model, input_tensors=None, clone_function=None):
  """Clone any `Model` instance.

  Model cloning is similar to calling a model on new inputs,
  except that it creates new layers (and thus new weights) instead
  of sharing the weights of the existing layers.

  Arguments:
      model: Instance of `Model`
          (could be a functional model or a Sequential model).
      input_tensors: optional list of input tensors or InputLayer objects
          to build the model upon. If not provided,
          placeholders will be created.
      clone_function: Callable to be used to clone each layer in the target
          model (except `InputLayer` instances). It takes as argument the layer
          instance to be cloned, and returns the corresponding layer instance to
          be used in the model copy. If unspecified, this callable defaults to
          the following serialization/deserialization function:
          `lambda layer: layer.__class__.from_config(layer.get_config())`.
          By passing a custom callable, you can customize your copy of the
          model, e.g. by wrapping certain layers of interest (you might want to
          replace all `LSTM` instances with equivalent
          `Bidirectional(LSTM(...))` instances, for example).

  Returns:
      An instance of `Model` reproducing the behavior
      of the original model, on top of new inputs tensors,
      using newly instantiated weights. The cloned model might behave
      differently from the original model if a custom clone_function
      modifies the layer.

  Raises:
      ValueError: in case of invalid `model` argument value.
  """
  if clone_function is None:
    clone_function = _clone_layer

  if isinstance(model, Sequential):
    return _clone_sequential_model(
        model, input_tensors=input_tensors, layer_fn=clone_function)
  else:
    return _clone_functional_model(
        model, input_tensors=input_tensors, layer_fn=clone_function)


# "Clone" a subclassed model by reseting all of the attributes.
def _in_place_subclassed_model_reset(model):
  """Substitute for model cloning that works for subclassed models.

  Subclassed models cannot be cloned because their topology is not serializable.
  To "instantiate" an identical model in a new TF graph, we reuse the original
  model object, but we clear its state.

  After calling this function on a model instance, you can use the model
  instance as if it were a model clone (in particular you can use it in a new
  graph).

  This method clears the state of the input model. It is thus destructive.
  However the original state can be restored fully by calling
  `_in_place_subclassed_model_state_restoration`.

  Args:
    model: Instance of a Keras model created via subclassing.

  Raises:
    ValueError: In case the model uses a subclassed model as inner layer.
  """
  assert not model._is_graph_network  # Only makes sense for subclassed networks
  # Retrieve all layers tracked by the model as well as their attribute names
  attributes_cache = {}
  for name in dir(model):
    try:
      value = getattr(model, name)
    except (AttributeError, ValueError, TypeError):
      continue
    if isinstance(value, Layer):
      attributes_cache[name] = value
      assert value in model.layers
      if hasattr(value, 'layers') and value.layers:
        raise ValueError('We do not support the use of nested layers '
                         'in `model_to_estimator` at this time. Found nested '
                         'layer: %s' % value)
    elif isinstance(
        value, (list, tuple)) and name not in ('layers', '_layers', 'metrics',
                                               '_compile_metric_functions',
                                               '_output_loss_metrics'):
      # Handle case: list/tuple of layers (also tracked by the Network API).
      if value and all(isinstance(val, Layer) for val in value):
        raise ValueError('We do not support the use of list-of-layers '
                         'attributes in subclassed models used with '
                         '`model_to_estimator` at this time. Found list '
                         'model: %s' % name)

  # Replace layers on the model with fresh layers
  layers_to_names = {value: key for key, value in attributes_cache.items()}
  original_layers = model._layers[:]
  setattr_tracking = model._setattr_tracking
  model._setattr_tracking = False
  model._layers = []
  for layer in original_layers:  # We preserve layer order.
    config = layer.get_config()
    # This will not work for nested subclassed models used as layers.
    # This would be theoretically possible to support, but would add complexity.
    # Only do it if users complain.
    if isinstance(layer, Network) and not layer._is_graph_network:
      raise ValueError('We do not support the use of nested subclassed models '
                       'in `model_to_estimator` at this time. Found nested '
                       'model: %s' % layer)
    fresh_layer = layer.__class__.from_config(config)
    name = layers_to_names[layer]
    setattr(model, name, fresh_layer)
    model._layers.append(fresh_layer)

  # Cache original model build attributes (in addition to layers)
  if (not hasattr(model, '_original_attributes_cache') or
      model._original_attributes_cache is None):
    if model.built:
      attributes_to_cache = [
          'inputs',
          'outputs',
          '_feed_outputs',
          '_feed_output_names',
          '_feed_output_shapes',
          '_feed_loss_fns',
          'loss_weights_list',
          'targets',
          '_feed_targets',
          'sample_weight_modes',
          'total_loss',
          'sample_weights',
          '_feed_sample_weights',
          'train_function',
          'test_function',
          'predict_function',
          '_collected_trainable_weights',
          '_feed_inputs',
          '_feed_input_names',
          '_feed_input_shapes',
          'optimizer',
      ]
      for name in attributes_to_cache:
        attributes_cache[name] = getattr(model, name)
  model._original_attributes_cache = attributes_cache
  _reset_build_compile_trackers(model)
  model._setattr_tracking = setattr_tracking


def _reset_build_compile_trackers(model):
  """Reset state trackers for model.

  Note that we do not actually zero out attributes such as optimizer,
  but instead rely on the expectation that all of the attrs will be
  over-written on calling build/compile/etc. This is somewhat fragile,
  insofar as we check elsewhere for the presence of these attributes as
  evidence of having been built/compiled/etc. Pending a better way to do this,
  we reset key attributes here to allow building and compiling.

  Args:
    model: the model that is being reset
  """
  # Reset build state
  model.built = False
  model.inputs = None
  model.outputs = None
  # Reset compile state
  model._is_compiled = False  # pylint:disable=protected-access
  model.optimizer = None


def in_place_subclassed_model_state_restoration(model):
  """Restores the original state of a model after it was "reset".

  This undoes this action of `_in_place_subclassed_model_reset`, which is called
  in `clone_and_build_model` if `in_place_reset` is set to True.

  Args:
    model: Instance of a Keras model created via subclassing, on which
      `_in_place_subclassed_model_reset` was previously called.
  """
  assert not model._is_graph_network
  # Restore layers and build attributes
  if (hasattr(model, '_original_attributes_cache') and
      model._original_attributes_cache is not None):
    # Models have sticky attribute assignment, so we want to be careful to add
    # back the previous attributes and track Layers by their original names
    # without adding dependencies on "utility" attributes which Models exempt
    # when they're constructed.
    setattr_tracking = model._setattr_tracking
    model._setattr_tracking = False
    model._layers = []
    for name, value in model._original_attributes_cache.items():
      setattr(model, name, value)
      if isinstance(value, Layer):
        model._layers.append(value)
    model._original_attributes_cache = None
    model._setattr_tracking = setattr_tracking
  else:
    # Restore to the state of a never-called model.
    _reset_build_compile_trackers(model)


def clone_and_build_model(
    model, input_tensors=None, target_tensors=None, custom_objects=None,
    compile_clone=True, in_place_reset=False, optimizer_iterations=None):
  """Clone a `Model` and build/compile it with the same settings used before.

  This function can be be run in the same graph or in a separate graph from the
  model. When using a separate graph, `in_place_reset` must be `False`.

  Note that, currently, the clone produced from this function may not work with
  TPU DistributionStrategy. Try at your own risk.

  Args:
    model: `tf.keras.Model` object. Can be Functional, Sequential, or
      sub-classed.
    input_tensors: Optional list of input tensors to build the model upon. If
      not provided, placeholders will be created.
    target_tensors: Optional list of target tensors for compiling the model. If
      not provided, placeholders will be created.
    custom_objects: Optional dictionary mapping string names to custom classes
      or functions.
    compile_clone: Boolean, whether to compile model clone (default `True`).
    in_place_reset: Boolean, whether to reset the model in place. Only used if
      the model is a subclassed model. In the case of a subclassed model,
      this argument must be set to `True` (default `False`). To restore the
      original model, use the function
      `in_place_subclassed_model_state_restoration(model)`.
    optimizer_iterations: An iterations variable that will be incremented by the
      optimizer if the clone is compiled. This argument is used when a Keras
      model is cloned into an Estimator model function, because Estimators
      create their own global step variable.

  Returns:
    Clone of the model.

  Raises:
    ValueError: Cloning fails in the following cases
      - cloning a subclassed model with `in_place_reset` set to False.
      - compiling the clone when the original model has not been compiled.
  """
  # Grab optimizer now, as we reset-in-place for subclassed models, but
  # want to maintain access to the original optimizer.
  orig_optimizer = model.optimizer
  if compile_clone and not orig_optimizer:
    raise ValueError(
        'Error when cloning model: compile_clone was set to True, but the '
        'original model has not been compiled.')

  if model._is_graph_network or isinstance(model, Sequential):
    if custom_objects:
      with CustomObjectScope(custom_objects):
        clone = clone_model(model, input_tensors=input_tensors)
    else:
      clone = clone_model(model, input_tensors=input_tensors)

    if all([isinstance(clone, Sequential),
            not clone._is_graph_network,
            getattr(model, '_build_input_shape', None) is not None]):
      # Set model inputs to build the model and add input/output properties.
      # TODO(kathywu): Add multiple placeholders to handle edge case where
      # sequential model has multiple inputs.
      clone._set_inputs(
          K.placeholder(model._build_input_shape, dtype=model.inputs[0].dtype))
  else:
    if not in_place_reset:
      raise ValueError(
          'This model is a subclassed model. '
          'Such a model cannot be cloned, but there is a workaround where '
          'the model is reset in-place. To use this, please set the argument '
          '`in_place_reset` to `True`. This will reset the attributes in the '
          'original model. To restore the attributes, call '
          '`in_place_subclassed_model_state_restoration(model)`.')
    clone = model
    _in_place_subclassed_model_reset(clone)
    if input_tensors is not None:
      if isinstance(input_tensors, (list, tuple)) and len(input_tensors) == 1:
        input_tensors = input_tensors[0]
      clone._set_inputs(input_tensors)

  if compile_clone:
    if isinstance(orig_optimizer, optimizers.TFOptimizer):
      optimizer = optimizers.TFOptimizer(
          orig_optimizer.optimizer, optimizer_iterations)
      K.track_tf_optimizer(optimizer)
    else:
      optimizer_config = orig_optimizer.get_config()
      optimizer = orig_optimizer.__class__.from_config(optimizer_config)
      if optimizer_iterations is not None:
        optimizer.iterations = optimizer_iterations

    clone.compile(
        optimizer,
        model.loss,
        metrics=metrics_module.clone_metrics(model._compile_metrics),
        loss_weights=model.loss_weights,
        sample_weight_mode=model.sample_weight_mode,
        weighted_metrics=metrics_module.clone_metrics(
            model._compile_weighted_metrics),
        target_tensors=target_tensors)

  return clone
