# -*- coding: utf-8 -*-
# lstm.py
#
# based on tensorflow 1.5.0 (needs Cuda 9.0 and CuDNN >7.1 support)
#
# author: Tony Tong (taotong@berkeley.edu, ttong@pro-ai.org)
#
# TODO: Write a more generalized data_feeder factory method to streamline different types of input data features.
# TODO: Write a high-level model auto-tuning wrapper to systematically explore hyperparameter space to optimize model.
# TODO: Add multiple GPU support for data parallelism (multiple graphs on different GPUs and trained on unified data
# TODO:     feeder.
# TODO: Experiment with accessing tensorflow cluster for large-scale parallelism.


import numpy as np
import random
import string
import os
from functools import partial
from time import time
import tensorflow as tf
from tensorflow.python.client import device_lib
from IPython.display import display, HTML
import logging


logger = logging.getLogger('gpu_compute')
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler('gpu_compute.log')
fh.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s::%(name)s::%(levelname)s %(message)s')
fh.setFormatter(formatter)
ch.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(ch)


def reset_graph(seed=42):
    """Reset default tensorflow graph"""
    tf.reset_default_graph()
    tf.set_random_seed(seed)
    np.random.seed(seed)


def strip_consts(graph_def, max_const_size=32):
    """Strip large constant values from graph_def."""
    strip_def = tf.GraphDef()
    for n0 in graph_def.node:
        n = strip_def.node.add()
        n.MergeFrom(n0)
        if n.op == 'Const':
            tensor = n.attr['value'].tensor
            size = len(tensor.tensor_content)
            if size > max_const_size:
                tensor.tensor_content = b"<stripped %d bytes>" % size
    return strip_def


def show_graph(graph_def=None, max_const_size=32):
    """Visualize TensorFlow graph within the notebook"""
    if graph_def is None:
        graph_def = tf.get_default_graph()
    if hasattr(graph_def, 'as_graph_def'):
        graph_def = graph_def.as_graph_def()
    strip_def = strip_consts(graph_def, max_const_size=max_const_size)
    code = """
        <script>
          function load() {{
            document.getElementById("{id}").pbtxt = {data};
          }}
        </script>
        <link rel="import" href="https://tensorboard.appspot.com/tf-graph-basic.build.html" onload=load()>
        <div style="height:600px">
          <tf-graph-basic id="{id}"></tf-graph-basic>
        </div>
    """.format(data=repr(str(strip_def)), id='graph' + str(np.random.rand()))
    iframe = """
        <iframe seamless style="width:1600px;height:800px;border:0" srcdoc="{}"></iframe>
    """.format(code.replace('"', '&quot;'))
    # This will display the graph inside the jupyter notebook
    display(HTML(iframe))


def id_generator(size=6, chars=string.ascii_uppercase + string.digits):
    return ''.join(random.choice(chars) for _ in range(size))


class LSTM:
    """Container for multi-time-step and multi-layered LSTM framework"""
    global logger

    def __init__(self, n_input_features=None, n_output_features=1, batch_size=None,
                 cell_type='LSTM', n_states=50, n_layers=1, use_peepholes=True, n_time_steps=10,
                 activation=tf.nn.relu, keep_prob=0.5, l1_reg=1e-2, l2_reg=1e-3,
                 start_learning_rate=0.001, decay_steps=1, decay_rate=0.3,
                 inner_iteration=10, forward_step=1, create_graph=True,
                 scope='lstm', log_dir='logs', model_dir='saved_models',
                 device='gpu', device_num=0, verbose=0):
        self.n_input_features = n_input_features
        self.n_output_features = n_output_features
        self.batch_size = batch_size
        self.cell_type = cell_type.upper()
        self.n_states = n_states
        self.n_layers = n_layers
        self.use_peepholes = use_peepholes  # this is a special feature for LSTM cell
        self.n_time_steps = n_time_steps
        self.activation = activation
        self.keep_prob = keep_prob
        self.l1_reg_scale = l1_reg
        self.l2_reg_scale = l2_reg
        self.start_learning_rate = start_learning_rate
        self.decay_steps = decay_steps
        self.decay_rate = decay_rate
        self.inner_iteration = inner_iteration
        self.forward_step = forward_step
        self.sessid = None
        self.device_list = None
        self.graph = None
        self.graph_keys = None
        self.graph_ready = False
        self.graph_trained = False
        self.trained_epochs = 0
        self.scope = scope
        self.log_dir = log_dir
        self.model_saver = None
        self.model_dir = model_dir

        # results holder
        self.all_actual_is = None
        self.all_actual_oos = None
        self.all_predicted_is = None
        self.all_predicted_oos = None
        self.all_epochs = []
        self.all_losses_per_epoch = []
        self.all_losses_oos_per_epoch = []
        self.all_corr_oos_per_epoch = []
        self.all_corr_is = []
        self.all_corr_oos = []
        self.cell_states = dict()  # dictionary holding the last layer rnn cell states
        self.fc_states = dict()  # dictionary holding the rnn to output fc layer states

        # Locate available computing devices and save in self.device_list
        self.device_list = self.find_compute_devices()
        if device.lower() == 'cpu':
            self.compute_device = self.device_list['cpu'][0]
        elif device.lower() == 'gpu':
            try:
                self.compute_device = self.device_list['gpu'][device_num]
            except (KeyError, IndexError) as msg:
                print(msg)
                print("Will try GPU device 0...")
            try:
                self.compute_device = self.device_list['gpu'][0]  # Try to grab the first gpu
            except (KeyError, IndexError) as msg:
                print(msg)
                print("Will default to CPU for computing")
                self.compute_device = self.device_list['cpu'][0]  # default to cpu as computing device

        # If create_graph is set to True, then create the graph directly during the initiation.
        # You can always reset_graph and recreate new ones later.
        if create_graph:
            try:
                self.create_lstm_graph(n_input_features=n_input_features, verbose=verbose)
                self.graph_ready = True
            except Exception as msg:
                print("Exception occurred during graph creation.  Check input parameters, especially n_input_features.")
                print("Exception message: ", msg)
                self.graph_ready = False

    def __call__(self, n_input_features=None, n_output_features=1, batch_size=None,
                 cell_type='LSTM', n_states=50, n_layers=1, use_peepholes=True, n_time_steps=10,
                 activation=tf.nn.relu, keep_prob=0.5, l1_reg=1e-2, l2_reg=1e-3,
                 start_learning_rate=0.001, decay_steps=1, decay_rate=0.3,
                 iter_per_id=10, forward_step=1, create_graph=True,
                 scope='lstm', log_dir='logs', model_dir='saved_models',
                 device='gpu', device_num=0, verbose=0):
        """A wrapper for calling the __init__ function"""

        if self.graph is not None:
            if verbose >= 1:
                print("Warning: current graph if defined will be lost. ")
            del self.graph

        self.__init__(n_input_features=n_input_features, n_output_features=n_output_features, batch_size=batch_size,
                      cell_type=cell_type, n_states=n_states, n_layers=n_layers, use_peepholes=True,
                      n_time_steps=n_time_steps,
                      activation=activation, keep_prob=keep_prob, l1_reg=l1_reg, l2_reg=l2_reg,
                      start_learning_rate=start_learning_rate, decay_steps=decay_steps, decay_rate=decay_rate,
                      inner_iteration=iter_per_id, forward_step=forward_step, create_graph=create_graph,
                      scope=scope, log_dir=log_dir, model_dir=model_dir,
                      device=device, device_num=device_num, verbose=verbose
                      )


    def logging_session_parameters(self, log=None):
        if log is None:
            log = logger
        log.info(f"[{self.sessid}] Session start")
        log.info(f"[{self.sessid}] Input features: {self.n_input_features}")
        log.info(f"[{self.sessid}] Output features: {self.n_output_features}")
        log.info(f"[{self.sessid}] Num of units in each {self.cell_type} cell: {self.n_states}")
        log.info(f"[{self.sessid}] Num of stacked {self.cell_type} layers: {self.n_layers}")
        log.info(f"[{self.sessid}] Num of unrolled time steps: {self.n_time_steps}")
        log.info(f"[{self.sessid}] Activation function: {self.activation.__name__}")
        log.info(f"[{self.sessid}] Dropout rate during training: {1 - self.keep_prob}")
        log.info(f"[{self.sessid}] L1 regularization: {self.l1_reg_scale}")
        log.info(f"[{self.sessid}] L2 regularization: {self.l2_reg_scale}")
        log.info(f"[{self.sessid}] Start learning rate: {self.start_learning_rate}")
        log.info(f"[{self.sessid}] Learning rate decay steps: {self.decay_steps}")
        log.info(f"[{self.sessid}] Learning rate decay rate: {self.decay_rate}")
        log.info(f"[{self.sessid}] Inner iterations: {self.inner_iteration}")
        log.info(f"[{self.sessid}] Forward prediction period: {self.forward_step}")


    @staticmethod
    def find_compute_devices():
        """Find available compute devices (gpu's and cpu's) on the local node and store them as a dictionary.
        Note:
            Tensorflow lumps all available cpu cores together as a single cpu resource.  GPU devices will be
            separate.
        """
        device_list = device_lib.list_local_devices()
        gpu, cpu = [], []
        for device in device_list:
            if device.name.find('GPU') != -1:
                gpu.append(device.name)
            if device.name.find('CPU') != -1:
                cpu.append(device.name)
        assert len(cpu) >= 1  # assert at least cpu resource is available
        return dict(gpu=gpu, cpu=cpu)


    def show_compute_devices(self):
        """Show available compute devices on the local node"""
        if self.compute_device is None:
            self.device_list = self.find_compute_devices()
        print("Following compute devices available\n  ", self.device_list)


    def set_compute_device(self, type='gpu', seq=0):
        """Set compute device for this tensorflow graph.
        Currently, the entire graph needs to stay in one device.  In the future, distributed graph across GPU's
        will be attempted.
        """
        try:
            self.compute_device = self.device_list[type][seq]
        except (KeyError, IndexError) as msg:
            print("Error in selecting target device, defaulting to CPU as compute device. \n"
                  "Please use show_compute_devices() to list available compute devices.")
            self.compute_device = self.device_list['cpu'][0]  # default to cpu as computing device


    def reset_graph(self):
        """Reset existing tensorflow graph and create an empty tf.Graph() object"""
        del self.graph
        self.graph = tf.Graph()


    def show_graph(self, max_const_size=32):
        """Show existing tensorflow graph inside jupyter notebook"""
        show_graph(self.graph.as_graph_def(), max_const_size)


    @staticmethod
    def get_tf_normal_variable(shape, mean=0.0, stddev=0.6, name=None):
        """Obtain a tf.Variable with desired shape and to be initialized with truncated normal
        distribution.
        """
        return tf.Variable(tf.truncated_normal(shape, mean=mean, stddev=stddev),
                           validate_shape=False, name=name)

    def create_lstm_graph(self, n_input_features=None, reset_graph=True, log=None, verbose=0):
        """Build the Tensorflow based LSTM network
        Input::
        n_input_features: number of input features, there is no default value and has to be provided.
        Return::  tensor references that need to be referenced later
        """
        if log is None:
            log = logger

        if n_input_features is None:
            assert self.n_input_features is not None
        else:
            self.n_input_features = n_input_features

        if self.graph is None:
            self.graph = tf.Graph()
        elif reset_graph:
            if verbose >= 1:
                print("Warning: current graph if defined will be lost. ")
            self.reset_graph()

        # Build the network on the specified compute device
        with tf.device(self.compute_device):
            # Set this graph as the default
            with self.graph.as_default():
                with tf.name_scope(self.scope):
                    # Define input placeholder X
                    with tf.name_scope('input'):
                        with tf.name_scope('X'):
                            # [None, n_time_steps, n_input_features]
                            # n_input_features should include all the inputs flattened into a vector
                            X = tf.placeholder(tf.float32, shape=[self.batch_size, self.n_time_steps,
                                                                  self.n_input_features], name='X')

                    with tf.name_scope('hyperparameters'):
                        with tf.name_scope('keep_prob'):
                            # Define keep_prob placeholder for dropout
                            keep_prob = tf.placeholder(tf.float32, name='keep_prob')

                        with tf.name_scope('in_sample_cutoff'):
                            # Split point between training and test
                            # Only the training portion will be included in the loss function calculation
                            in_sample_cutoff = tf.placeholder(tf.int32, shape=(), name='in_sample_cutoff')

                    # Define multilayer LSTM network
                    with tf.name_scope('model'):
                        with tf.name_scope('rnn'):
                            # Adding dropout wrapper layer and LSTM cells with the number of hidden units
                            # in each LSTMCell as n_states.
                            # We have disabled the use_peepholes for now, can experiment its effect in the future.
                            if self.cell_type == 'LSTM':
                                lstm_layers = [
                                    tf.nn.rnn_cell.DropoutWrapper(
                                        tf.nn.rnn_cell.LSTMCell(num_units=self.n_states,
                                                                use_peepholes=self.use_peepholes,
                                                                forget_bias=1.0, activation=self.activation,
                                                                state_is_tuple=True),
                                        output_keep_prob=self.keep_prob
                                    )
                                    for _ in range(self.n_layers)
                                ]
                                multilayer_cell = tf.nn.rnn_cell.MultiRNNCell(lstm_layers, state_is_tuple=True)

                            elif self.cell_type == 'GRU':
                                gru_layers = [
                                    tf.nn.rnn_cell.DropoutWrapper(
                                        tf.nn.rnn_cell.GRUCell(num_units=self.n_states, activation=self.activation),
                                        output_keep_prob=self.keep_prob
                                    )
                                    for _ in range(self.n_layers)
                                ]
                                multilayer_cell = tf.nn.rnn_cell.MultiRNNCell(gru_layers, state_is_tuple=True)

                            elif self.cell_type == 'RNN':
                                rnn_layers = [
                                    tf.nn.rnn_cell.DropoutWrapper(
                                        tf.nn.rnn_cell.BasicRNNCell(num_units=self.n_states,
                                                                    activation=self.activation),
                                        output_keep_prob=self.keep_prob
                                    )
                                    for _ in range(self.n_layers)
                                ]
                                multilayer_cell = tf.nn.rnn_cell.MultiRNNCell(rnn_layers, state_is_tuple=True)
                            else:
                                assert False, f"cell_type {self.cell_type} is not recognized"

                        with tf.name_scope('dynamical_unrolling'):
                            # init_states = []
                            # for _ in range(len(lstm_layers)):
                            #     cell_state = get_tf_normal_variable((batch_size, n_states))
                            #     hidden_state = get_tf_normal_variable((batch_size, n_states))
                            #     state_tuple = tf.nn.rnn_cell.LSTMStateTuple(cell_state, hidden_state)
                            #     init_states.append(state_tuple)

                            # Use dynamic_rnn to dynamically unroll the time steps when doing the computation
                            # outputs contain the output from all the time steps, so it should have
                            # shape [batch_size, n_time_steps, n_states]
                            # When time_major is set to True, the outputs shape should be
                            # [n_time_steps, batch_size, n_states]
                            # states contain the all the internal states at the last time step.
                            # It is a tuple with elements corresponding to n_layers. Each tuple element itself
                            # is a LSTMStateTuple with c and h tensors.
                            outputs, states = tf.nn.dynamic_rnn(cell=multilayer_cell, inputs=X,
                                                                initial_state=None, dtype=tf.float32,
                                                                swap_memory=True, time_major=False)

                        # Use a fully-connected layer to convert the multi-state vector into a single
                        # scalar representing the variable to be predicted
                        with tf.name_scope('fc1'):
                            with tf.name_scope('W'):
                                W_fc1 = self.get_tf_normal_variable([self.n_states, self.n_output_features],
                                                                    name='W_fc1')
                            with tf.name_scope('b'):
                                b_fc1 = self.get_tf_normal_variable([self.n_output_features], name='b_fc1')
                            with tf.name_scope('pred'):
                                # states[-1][1] is the h states of the last layer LSTM cell
                                if self.cell_type == 'LSTM':
                                    pred = tf.matmul(states[-1][1], W_fc1) + b_fc1  # [None, n_output_features]
                                else:
                                    pred = tf.matmul(states[-1], W_fc1) + b_fc1  # [None, n_output_features]

                    # Placeholder for the output (label)
                    with tf.name_scope('label'):
                        # y has shape [None, n_output_features]
                        y = tf.placeholder(tf.float32, shape=[self.batch_size, self.n_output_features], name='y_label')
                        # this is important - we only want to train on the in-sample set of rows using TensorFlow
                        y_is = y[0:in_sample_cutoff, :]
                        pred_is = pred[0:in_sample_cutoff, :]
                        # also extract out of sample predictions and actual values,
                        # we'll use them for evaluation while training the model.
                        y_oos = y[in_sample_cutoff:, :]
                        pred_oos = pred[in_sample_cutoff:, :]

                    with tf.name_scope('stats'):
                        epsilon = 1.e-4
                        # Pearson correlation to evaluate the model, here is for in-sample training data
                        covariance_is = tf.matmul(
                            tf.transpose(tf.subtract(pred_is, tf.reduce_mean(pred_is, axis=0, keepdims=True))),
                            tf.subtract(y_is, tf.reduce_mean(y_is, axis=0, keepdims=True))
                        )  # covariance matrix, shape [n_output_features, n_output_features]
                        var_pred_is = tf.reduce_sum(
                            tf.square(tf.subtract(pred_is, tf.reduce_mean(pred_is))), axis=0, keepdims=True
                        )  # variance of pred_is, shape [1, n_output_features]
                        var_y_is = tf.reduce_sum(
                            tf.square(tf.subtract(y_is, tf.reduce_mean(y_is))), axis=0, keepdims=True
                        )  # variance of y_is, shape [1, n_output_features]
                        # pearson correlation matrix, shape [n_output_features, n_output_features]
                        pearson_corr_is = tf.div(
                            covariance_is, tf.sqrt(tf.matmul(tf.transpose(var_pred_is), var_y_is)) + epsilon,
                            name='pearson_corr_is'
                        )

                        # Pearson correlation for out-of-sample data
                        covariance_oos = tf.matmul(
                            tf.transpose(tf.subtract(pred_oos, tf.reduce_mean(pred_oos, axis=0, keepdims=True))),
                            tf.subtract(y_oos, tf.reduce_mean(y_oos, axis=0, keepdims=True))
                        )  # covariance matrix, shape [n_output_features, n_output_features]
                        var_pred_oos = tf.reduce_sum(
                            tf.square(tf.subtract(pred_oos, tf.reduce_mean(pred_oos))), axis=0, keepdims=True
                        )  # variance of pred_oos, shape [1, n_output_features]
                        var_y_oos = tf.reduce_sum(
                            tf.square(tf.subtract(y_oos, tf.reduce_mean(y_oos))), axis=0, keepdims=True
                        )  # variance of y_oos, shape [1, n_output_features]
                        # pearson correlation matrix, shape [n_output_features, n_output_features]
                        pearson_corr_oos = tf.div(
                            covariance_oos, tf.sqrt(tf.matmul(tf.transpose(var_pred_oos), var_y_oos)) + epsilon,
                            name='pearson_corr_oos'
                        )

                    with tf.name_scope('hyperparameters'):
                        # set up adaptive learning rate:
                        # Ratio of global_step / decay_steps is designed to indicate how far we've
                        # progressed in training.
                        # the ratio is 0 at the beginning of training and is 1 at the end.
                        global_step = tf.placeholder(tf.float32, name='global_step')

                        # tf.train.exponetial_decay is calculated as:
                        #     decayed_learning_rate = learning_rate * decay_rate ^ (global_step / decay_steps)
                        # adaptive_learning_rate will thus change from the starting learningRate to
                        # learningRate * decay_rate
                        # in order to simplify the code, we are fixing the total number of decay steps at 1
                        # and pass global_step as a fraction that starts with 0 and tends to 1.
                        adaptive_learning_rate = tf.train.exponential_decay(
                            learning_rate=self.start_learning_rate,  # Start with this learning rate
                            global_step=global_step,  # global_step/total_steps shows progress in training
                            decay_steps=self.decay_steps,
                            decay_rate=self.decay_rate
                        )

                    # Define loss and optimizer
                    # Note the loss only involves in-sample rows
                    # Regularization is added in the loss function to avoid over-fitting
                    # lstm_variables = [v for v in tf.trainable_variables()
                    #                                   if v.name.startswith('rnn')]

                    with tf.name_scope('loss'):
                        # this the loss function for optimization purpose
                        loss = tf.nn.l2_loss(tf.subtract(y_is, pred_is)) + \
                               tf.contrib.layers.apply_regularization(
                                   tf.contrib.layers.l1_l2_regularizer(
                                       scale_l1=self.l1_reg_scale,
                                       scale_l2=self.l2_reg_scale
                                   ),
                                   tf.trainable_variables()
                               )

                        # this is the out-of-sample L2 loss, only for observation, never use for optimization
                        loss_oos = tf.nn.l2_loss(tf.subtract(y_oos, pred_oos))

                    with tf.name_scope('optimizer'):
                        optimizer = tf.train.AdamOptimizer(learning_rate=adaptive_learning_rate).minimize(loss)

                    with tf.name_scope('init'):
                        init = tf.global_variables_initializer()

                    with tf.name_scope('summary'):
                        tf.summary.tensor_summary("pearson_corr_is", pearson_corr_is)
                        tf.summary.tensor_summary("pearson_corr_oos", pearson_corr_oos)
                        tf.summary.scalar("loss", loss)
                        tf.summary.scalar("validation_loss", loss_oos)
                        summary_op = tf.summary.merge_all()

                    # Write the graph to summary
                    try:
                        writer = tf.summary.FileWriter(self.log_dir, graph=tf.get_default_graph())
                    except Exception as msg:
                        writer = None
                        log.exception("Exception when saving summary info: ", msg)

                    self.model_saver = tf.train.Saver()

                    # Group all the keys into a dictionary by using kwargs
                    self.graph_keys = dict(
                        X=X,
                        y=y,
                        y_is=y_is,
                        y_oos=y_oos,
                        pred=pred,
                        pred_is=pred_is,
                        pred_oos=pred_oos,
                        keep_prob=keep_prob,
                        in_sample_cutoff=in_sample_cutoff,
                        global_step=global_step,
                        states=states,
                        outputs=outputs,
                        loss=loss,
                        loss_oos=loss_oos,
                        optimizer=optimizer,
                        pearson_corr_is=pearson_corr_is,
                        pearson_corr_oos=pearson_corr_oos,
                        adaptive_learning_rate=adaptive_learning_rate,
                        init=init,
                        summary_op=summary_op,
                        writer=writer
                    )
    # end create_lstm_graph

    def clear_prev_results(self):
        self.all_actual_is = None
        self.all_actual_oos = None
        self.all_predicted_is = None
        self.all_predicted_oos = None
        self.all_epochs = []
        self.all_losses_per_epoch = []
        self.all_losses_oos_per_epoch = []
        self.all_corr_oos_per_epoch = []
        self.all_corr_is = []
        self.all_corr_oos = []
        self.cell_states = dict()  # dictionary holding the last layer rnn cell states
        self.fc_states = dict()  # dictionary holding the rnn to output fc layer states

    def train(self, batch_X=None, batch_y=None, in_sample_size=None, y_is_mean=0.0, y_is_std=1.0, data_feeder=None,
              restore_model=True, pre_trained_model=None, epoch_prev=0, epoch_end=21, inner_iteration=None,
              step=1, writer_step=1, display_step=50, return_weights=False, log=None, verbose=0):
        """Perform training of the LSTM network on specified compute device.
        There are two ways of feeding in data:

        (1) Directly pass in batch_X, batch_y, in_sample_size;
        (2) Passing in a data_feeder as a data generator.

        If both are provided, the direct data will take precedence over data_feeder.

        Model persistence:
            Model persistence is also built into the class.  As long as model_saver and model_dir are
            properly setup, they

        Return: A compiled dictionary of various outputs from training.
        """
        if log is None:
            log = logger

        if self.graph is None:
            log.warning("No graph available for training.  Need to create compute graph first.")
            return None

        # If batch data are specifically provided, it will take priority over data_feeder
        if batch_X is not None and batch_y is not None and in_sample_size is not None \
                and y_is_mean is not None and y_is_std is not None:
            def data_feeder():
                return [(batch_X, batch_y, y_is_mean, y_is_std, in_sample_size, total_sample_size)]

        # Launch a tensorflow compute session
        if self.compute_device.find('CPU') != -1:
            config = tf.ConfigProto(device_count={'GPU': 0}, allow_soft_placement=True, log_device_placement=True)
        else:
            config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=True)
        with tf.Session(graph=self.graph, config=config) as sess:
            # Restore latest checkpoint
            if restore_model:
                if pre_trained_model is not None:
                    try:
                        self.model_saver.restore(sess, os.path.join(self.model_dir, pre_trained_model))
                    except Exception as msg:
                        log.exception("Session restore failed: ", msg)
                        raise Exception(msg)
                    if self.sessid is None:
                        self.sessid = id_generator()
                    i = epoch_prev + 1
                    log.info(f"[{self.sessid}] Restored pre-trained model successfully")
                    self.logging_session_parameters()
                elif self.graph_trained:  # pre_trained_model is None and graph has been trained in a previous session
                    i = self.trained_epochs + 1
                    assert self.sessid is not None
                    try:
                        self.model_saver.restore(sess, os.path.join(self.model_dir, f"{self.sessid}_latest.ckpt"))
                    except Exception as msg:
                        log.exception("Active session restore failed: ", msg)
                        raise Exception
                    log.info(f"[{self.sessid}] Restored model parameters from previous active session.")
                    self.logging_session_parameters()
                else:
                    self.clear_prev_results()
                    epoch_prev = 0
                    sess.run(self.graph_keys['init'])
                    self.sessid = id_generator()
                    self.logging_session_parameters()
                    i = epoch_prev + 1  # set epoch counter
            else:
                self.clear_prev_results()
                epoch_prev = 0
                sess.run(self.graph_keys['init'])
                self.sessid = id_generator()
                self.logging_session_parameters()
                i = epoch_prev + 1  # set epoch counter

            while i <= epoch_end:
                log.info(f"[{self.sessid}] Epoch {i} Starts ******************************************************")
                tic = time()
                actual_oos = None  # resets for each epoch
                predicted_oos = None
                loss_epoch = 0.0
                loss_oos_epoch = 0.0  # validation set loss

                for batch_X, batch_y, y_is_mean, y_is_std, batch_y_index, in_sample_size, batch_id in data_feeder():
                    total_sample_size = batch_X.shape[0]
                    if np.isfinite(batch_X).sum() != batch_X.size or \
                       np.isfinite(batch_y).sum() != batch_y.size:
                        continue
                    assert np.isfinite(y_is_mean).sum() == y_is_mean.size
                    assert np.isfinite(y_is_std).sum() == y_is_std.size
                    assert in_sample_size <= total_sample_size, "in_sample_size needs to be smaller than total"
                    assert batch_X.shape[2] == self.n_input_features, \
                        "batch_X should have shape [batch_size, n_time_steps, n_input_features]"
                    assert batch_y.shape[1] == self.n_output_features, \
                        "batch_y should have shape [batch_size, n_output_features]"

                    # if inner_iteration is passed in, then it takes priority over internal state
                    if inner_iteration is None:
                        inner_iteration = self.inner_iteration
                    assert isinstance(inner_iteration, int) and inner_iteration >= 1, "inner_iteration has to be >= 1"

                    # Run optimization
                    # Note: dropout is intended for training only
                    for _ in range(inner_iteration):
                        _, current_rate, states_val = sess.run(
                            [
                                self.graph_keys['optimizer'],
                                self.graph_keys['adaptive_learning_rate'],
                                self.graph_keys['states']
                            ],
                            feed_dict={
                                self.graph_keys['X']: batch_X,
                                self.graph_keys['y']: batch_y,
                                self.graph_keys['keep_prob']: self.keep_prob,  # for training only
                                self.graph_keys['in_sample_cutoff']: in_sample_size,
                                self.graph_keys['global_step']: i / epoch_end
                            }
                        )
                    if verbose >= 3:  # DEBUG
                        print("states_val", states_val)
                    # Obtain out of sample target variable and prediction
                    y_val, pred_val, y_oos_val, pred_oos_val, \
                        pearson_corr_is_val, pearson_corr_oos_val, loss_val, loss_oos_val, summary = sess.run(
                        [
                            self.graph_keys['y'],
                            self.graph_keys['pred'],
                            self.graph_keys['y_oos'],
                            self.graph_keys['pred_oos'],
                            self.graph_keys['pearson_corr_is'],
                            self.graph_keys['pearson_corr_oos'],
                            self.graph_keys['loss'],
                            self.graph_keys['loss_oos'],
                            self.graph_keys['summary_op']
                        ],
                            feed_dict={
                            self.graph_keys['X']: batch_X,
                            self.graph_keys['y']: batch_y,
                            self.graph_keys['keep_prob']: 1.0,  # needs to be 1.0 for prediction
                            self.graph_keys['in_sample_cutoff']: in_sample_size
                        }
                    )
                    if verbose >= 3:  # DEBUG
                        print("y_val", y_val)
                        print("pred_val", pred_val)
                    assert (y_val[in_sample_size:, ] == y_oos_val).all(), \
                        "y_val and y_oos_val fails, likely nan."
                    assert (pred_val[in_sample_size:, ] == pred_oos_val).all(), \
                        "pred_val and pred_oos_val fails, likely nan."

                    # reverse transform before recording the results
                    # if no reverse transform is desired inside training, use default y_is_mean=0.0 and y_is_std=1.0
                    y_val = np.array(y_val) * y_is_std + y_is_mean
                    y_oos_val = np.array(y_oos_val) * y_is_std + y_is_mean
                    pred_val = np.array(pred_val) * y_is_std + y_is_mean
                    pred_oos_val = np.array(pred_oos_val) * y_is_std + y_is_mean

                    # record the results
                    if actual_oos is None:
                        actual_oos = np.array(y_oos_val)
                    else:
                        actual_oos = np.r_[actual_oos, np.array(y_oos_val)]

                    if predicted_oos is None:
                        predicted_oos = np.array(pred_oos_val)
                    else:
                        predicted_oos = np.r_[predicted_oos, np.array(pred_oos_val)]

                    if self.all_actual_is is None:
                        self.all_actual_is = np.array(y_val[0:in_sample_size, :])
                    else:
                        self.all_actual_is = np.r_[self.all_actual_is, np.array(y_val[0:in_sample_size, :])]

                    if self.all_actual_oos is None:
                        self.all_actual_oos = np.array(y_oos_val)
                    else:
                        self.all_actual_oos = np.r_[self.all_actual_oos, np.array(y_oos_val)]

                    if self.all_predicted_is is None:
                        self.all_predicted_is = np.array(pred_val[0:in_sample_size, :])
                    else:
                        self.all_predicted_is = np.r_[self.all_predicted_is, np.array(pred_val[0:in_sample_size, :])]

                    if self.all_predicted_oos is None:
                        self.all_predicted_oos = np.array(pred_oos_val)
                    else:
                        self.all_predicted_oos = np.r_[self.all_predicted_oos, np.array(pred_oos_val)]

                    pearson_corr_is_val = np.diagonal(pearson_corr_is_val).mean()  # Taking the mean of all outputs
                    pearson_corr_oos_val = np.diagonal(pearson_corr_oos_val).mean()  # Taking the mean of all outputs

                    self.all_corr_is.append(pearson_corr_is_val)
                    self.all_corr_oos.append(pearson_corr_oos_val)
                    loss_epoch += loss_val
                    loss_oos_epoch += loss_oos_val

                    if return_weights:
                        if self.cell_type == 'LSTM':
                            lstm_kernel_weights = self.graph.get_tensor_by_name(
                                f'rnn/multi_rnn_cell/cell_{self.n_layers-1}/lstm_cell/kernel:0'
                            )
                            lstm_kernel_biases = self.graph.get_tensor_by_name(
                                f'rnn/multi_rnn_cell/cell_{self.n_layers-1}/lstm_cell/bias:0'
                            )
                            self.cell_states['lstm_kernel_weights'] = lstm_kernel_weights.eval()
                            self.cell_states['lstm_kernel_biases'] = lstm_kernel_biases.eval()

                        elif self.cell_type == 'GRU':
                            gru_gates_weights = self.graph.get_tensor_by_name(
                                f'rnn/multi_rnn_cell/cell_{self.n_layers-1}/gru_cell/gates/kernel:0'
                            )
                            gru_gates_biases = self.graph.get_tensor_by_name(
                                f'rnn/multi_rnn_cell/cell_{self.n_layers-1}/gru_cell/gates/bias:0'
                            )
                            gru_candidate_weights = self.graph.get_tensor_by_name(
                                f'rnn/multi_rnn_cell/cell_{self.n_layers-1}/gru_cell/candidate/kernel:0'
                            )
                            gru_candidate_biases = self.graph.get_tensor_by_name(
                                f'rnn/multi_rnn_cell/cell_{self.n_layers-1}/gru_cell/candidate/bias:0'
                            )
                            self.cell_states['gru_gates_weights'] = gru_gates_weights.eval()
                            self.cell_states['gru_gates_biases'] = gru_gates_biases.eval()
                            self.cell_states['gru_candidate_weights'] = gru_candidate_weights.eval()
                            self.cell_states['gru_candidate_biases'] = gru_candidate_biases.eval()

                        elif self.cell_type == 'RNN':
                            rnn_kernel_weights = self.graph.get_tensor_by_name(
                                f'rnn/multi_rnn_cell/cell_{self.n_layers-1}/basic_rnn_cell/kernel:0'
                            )
                            rnn_kernel_biases = self.graph.get_tensor_by_name(
                                f'rnn/multi_rnn_cell/cell_{self.n_layers-1}/basic_rnn_cell/bias:0'
                            )
                            self.cell_states['rnn_kernel_weights'] = rnn_kernel_weights.eval()
                            self.cell_states['rnn_kernel_biases'] = rnn_kernel_biases.eval()
                        else:
                            assert False

                        # fully-connected layer between last rnn cell to output
                        fc_weights = self.graph.get_tensor_by_name(
                            f'{self.scope}/model/fc1/W/W_fc1:0'
                        )
                        fc_biases = self.graph.get_tensor_by_name(
                            f'{self.scope}/model/fc1/b/b_fc1:0'
                        )
                        self.fc_states['weights'] = fc_weights.eval()
                        self.fc_states['biases'] = fc_biases.eval()

                    # Once every display_step show some diagnostics - the loss function, in-sample correlation, etc.
                    if step % display_step == 0:
                        #                 print("add writer step to summary")
                        self.graph_keys['writer'].add_summary(summary, writer_step)
                        writer_step += 1
                        toc = time() - tic
                        log.info(
                            f"[{self.sessid}] Iter:{step}, LR:{current_rate:.5f}, mbatch_id: {batch_id}, "
                            f"loss: {loss_val:.1f}, validation loss: {loss_oos_val:.1f}, "
                            f"mbatch in-sample corr:{pearson_corr_is_val:7.4f}, oos corr:{pearson_corr_oos_val:7.4f} "
                            f"({in_sample_size}/{total_sample_size})), {toc:.1f}s elapsed."
                        )
                    step += 1  # finishes this id, continue to next id step

                # epoch finishes
                assert actual_oos.shape == predicted_oos.shape
                if verbose >= 2:
                    print("actual shape: ", actual_oos.shape)
                    print("predicted shape: ", predicted_oos.shape)

                corr_epoch = []
                for j in range(actual_oos.shape[1]):  # loop through n_output_features
                    corr_epoch.append(np.corrcoef(actual_oos[:, j], predicted_oos[:, j])[0, 1])
                corr_epoch_oos = np.array(corr_epoch).mean()

                if verbose >= 2:
                    print(corr_epoch_oos)

                self.all_epochs.append(i)
                self.all_losses_per_epoch.append(loss_epoch)
                self.all_losses_oos_per_epoch.append(loss_oos_epoch)
                self.all_corr_oos_per_epoch.append(corr_epoch_oos)
                log.info(
                    f'[{self.sessid}] Epoch {i}: total loss: {loss_epoch:.1f}, validation loss: {loss_oos_epoch:.1f}, '
                    f'total oos pearson corr: {corr_epoch_oos:8.5f}'
                )
                log.info(
                    f"[{self.sessid}] Epoch {i} Ends ======================================================"
                )
                try:
                    _ = self.model_saver.save(sess, os.path.join(self.model_dir, f"{self.sessid}_epoch_{i}.ckpt"))
                    _ = self.model_saver.save(sess, os.path.join(self.model_dir, f"{self.sessid}_latest.ckpt"))
                    log.info("Model checkpoint successfully saved.")
                except Exception:
                    log.info("Model checkpoint save unsuccessful")
                i += 1  # onto next epoch

            self.graph_trained = True  # indicating the current graph has been trained in the active session
            self.trained_epochs = i - 1
            results = dict(
                all_epochs=self.all_epochs,
                all_losses_per_epoch=self.all_losses_per_epoch,
                all_losses_oos_per_epoch=self.all_losses_oos_per_epoch,
                all_corr_oos_per_epoch=self.all_corr_oos_per_epoch,
                all_corr_is=self.all_corr_is,
                all_corr_oos=self.all_corr_oos,
                all_actual_is=self.all_actual_is,
                all_actual_oos=self.all_actual_oos,
                all_predicted_is=self.all_predicted_is,
                all_predicted_oos=self.all_predicted_oos
            )

            if return_weights:
                results.update(
                    dict(
                        cell_states=self.cell_states,
                        fc_states=self.fc_states
                    )
                )

            return results
    # end train

    def predict(self, batch_X=None, data_feeder=None, pre_trained_model=None, device=None, log=None):
        """Use trained model or restore from pre-trained model to predict
        Note: if a generator is passed in, the tensorflow Session will hold resources active until iterating
        through the entire iterable dataset.
        Set device to 'cpu' to use CPU for prediction
        Return/Yield: predicted values
        """
        if log is None:
            log = logger

        if self.graph is None:
            log.warning("No graph available for prediction.  Need to have a compute graph trained weights or "
                        "to be loaded from pre-trained saved model.")
            return None

        # If batch data are specifically provided, it will take priority over data_feeder
        if batch_X is not None:
            def data_feeder():
                return [batch_X]

        # Launch a tensorflow compute session
        if device is None:
            device = self.compute_device

        if device.upper().find('CPU') != -1:
            config = tf.ConfigProto(device_count={'GPU': 0}, allow_soft_placement=True, log_device_placement=True)
        else:
            config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=True)
        with tf.Session(graph=self.graph, config=config) as sess:
            # Restore latest checkpoint
            if pre_trained_model is not None:
                try:
                    self.model_saver.restore(sess, os.path.join(self.model_dir, pre_trained_model))
                except Exception as msg:
                    log.exception("Session restore failed: ", msg)
                    raise Exception(msg)
                if self.sessid is None:
                    self.sessid = id_generator()
                    log.info(f"[{self.sessid}] New sessid created for restored pre-trained model")
                log.info(f"[{self.sessid}] Restored pre-trained model successfully")
            else:
                assert self.sessid is not None, \
                    "No valid session id (sessid) from training in this active session. "\
                    "Alternatively, you may try restoring a previously trained model specifically."
                # Restore graph variables from training using default model persistence
                self.model_saver.restore(sess, os.path.join(self.model_dir, f"{self.sessid}_latest.ckpt"))
            self.logging_session_parameters()

            for data in data_feeder():
                # In the case that training feeder is used to feed data, we only take the first input, batch_X
                if isinstance(data, tuple):
                    batch_X, batch_y, y_mean, y_astd, y_index, in_sample_size, this_gvkey = data
                else:
                    batch_X = data
                    batch_y, y_mean, y_astd, y_index, in_sample_size, this_gvkey = None, None, None, None, None, None

                total_sample_size = batch_X.shape[0]
                # Obtain out of sample target variable and prediction
                pred_val = sess.run(
                    [
                        self.graph_keys['pred'],
                    ],
                    feed_dict={
                        self.graph_keys['X']: batch_X,
                        self.graph_keys['keep_prob']: 1.0,  # needs to be 1.0 for prediction
                        self.graph_keys['in_sample_cutoff']: total_sample_size
                    }
                )
                # log.info(f'[{self.sessid}] Prediction run successful.')

                batch_y = batch_y * y_astd + y_mean
                pred_val = np.array(pred_val) * y_astd + y_mean
                yield pred_val, batch_X, batch_y, y_mean, y_astd, y_index, this_gvkey
    # end predict


# using tf.nn.rnn_cell.GRUCell
GRU = partial(LSTM, cell_type='GRU', scope='gru')

# using tf.nn.rnn_cell.BasicRNNCell
RNN = partial(LSTM, cell_type='RNN', scope='rnn')


def test():
    # Model Network Parameters
    batch_size = None
    n_input_features = 10
    n_output_features = 10
    n_states = 30
    n_layers = 2
    n_time_steps = 20  # Number of unrolled time steps
    activation_fn = tf.nn.relu
    keep_prob_rate = 0.5  # keep_prob = 1 - dropout, for training only
    l2_reg_scale = 100e-3
    l1_reg_scale = 500e-3

    # Hyperparameters
    start_learning_rate = 0.0010
    decay_steps = 1
    decay_rate = 0.15
    inner_iteration = 20
    forward_step = 3

    # Build recurrent neural network
    g = LSTM(n_input_features=n_input_features, n_output_features=n_output_features, batch_size=batch_size,
             n_states=n_states, n_layers=n_layers, n_time_steps=n_time_steps,
             activation=activation_fn, keep_prob=keep_prob_rate, l1_reg=l1_reg_scale, l2_reg=l2_reg_scale,
             start_learning_rate=start_learning_rate, decay_steps=decay_steps, decay_rate=decay_rate,
             inner_iteration=inner_iteration, forward_step=forward_step, device='cpu', device_num=0, verbose=1)

    with tf.Session(graph=g.graph, config=tf.ConfigProto(allow_soft_placement=True, log_device_placement=True)) as sess:
        sess.run(g.graph_keys['init'])


if __name__ == "__main__":
    test()
