# Copyright 2016 The TensorFlow Authors All Rights Reserved.
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
"""Code for training the prediction model."""

import numpy as np
import tensorflow as tf

from tensorflow.python.platform import app
from tensorflow.python.platform import flags

from prediction_input_flo_chair import build_tfrecord_input, DATA_DIR
from prediction_model_flo_chair_pyramid import construct_model
from visualize import plot_flo_learn_symm, plot_autoencoder, plot_general
from optical_flow_warp import transformer
from optical_flow_warp_fwd import transformerFwd

import os

# How often to record tensorboard summaries.
SUMMARY_INTERVAL = 20

# How often to run a batch through the validation model.
VAL_INTERVAL = 200

# How often to save a model checkpoint
SAVE_INTERVAL = 500

FLAGS = flags.FLAGS

flags.DEFINE_string('data_dir', DATA_DIR, 'directory containing data.')
flags.DEFINE_string('output_dir', "", 'directory for model checkpoints.')
flags.DEFINE_integer('num_iterations', 100000, 'number of training iterations.')
flags.DEFINE_string('pretrained_model', '',
                    'filepath of a pretrained model to initialize from.')

flags.DEFINE_float('train_val_split', 0.95,
                   'The percentage of files to use for the training set,'
                   ' vs. the validation set.')

flags.DEFINE_integer('batch_size', 32, 'batch size for training')
flags.DEFINE_float('learning_rate', 0.001,
                   'the base learning rate of the generator')
flags.DEFINE_integer('num_gpus', 1,
                   'the number of gpu to use')


def get_black_list(clses):
  blacklist = []
  for cls in clses:
    fname = "/home/wangyang59/Data/ILSVRC2016/ImageSets/VID/train_%s.txt" % cls
    with open(fname) as f:
      content = f.readlines()
    blacklist += [x.split(" ")[0].split("/")[-1] + ".tfrecord" for x in content]
  return blacklist

## Helper functions
def peak_signal_to_noise_ratio(true, pred):
  """Image quality metric based on maximal signal power vs. power of the noise.

  Args:
    true: the ground truth image.
    pred: the predicted image.
  Returns:
    peak signal to noise ratio (PSNR)
  """
  return 10.0 * tf.log(1.0 / mean_squared_error(true, pred)) / tf.log(10.0)


def mean_squared_error(true, pred):
  """L2 distance between tensors true and pred.

  Args:
    true: the ground truth image.
    pred: the predicted image.
  Returns:
    mean squared error between ground truth and predicted image.
  """
  return tf.reduce_sum(tf.square(true - pred)) / tf.to_float(tf.size(pred))

def mean_charb_error(true, pred, beta):
  return tf.reduce_sum(tf.sqrt((tf.square(beta*(true-pred)) + 0.001*0.001))) / tf.to_float(tf.size(pred))

def mean_charb_error_wmask(true, pred, mask, beta):
  return tf.reduce_sum(tf.sqrt((tf.square(beta*(true-pred)) + 0.001*0.001))*mask) / tf.to_float(tf.size(pred))


def weighted_mean_squared_error(true, pred, weight):
  """L2 distance between tensors true and pred.

  Args:
    true: the ground truth image.
    pred: the predicted image.
  Returns:
    mean squared error between ground truth and predicted image.
  """
  
  tmp = tf.reduce_sum(weight*tf.square(true-pred), axis=[1,2], keep_dims=True) / tf.reduce_sum(weight, axis=[1, 2], keep_dims=True)
  return tf.reduce_mean(tmp)
  #return tf.reduce_sum(tf.square(true - pred)*weight) / tf.to_float(tf.size(pred))
  #return tf.reduce_sum(tf.square(true - pred)*weight) / tf.reduce_sum(weight)

def mean_L1_error(true, pred):
  """L2 distance between tensors true and pred.

  Args:
    true: the ground truth image.
    pred: the predicted image.
  Returns:
    mean squared error between ground truth and predicted image.
  """
  return tf.reduce_sum(tf.abs(true - pred)) / tf.to_float(tf.size(pred))

def weighted_mean_L1_error(true, pred, weight):
  """L2 distance between tensors true and pred.

  Args:
    true: the ground truth image.
    pred: the predicted image.
  Returns:
    mean squared error between ground truth and predicted image.
  """
  return tf.reduce_sum(tf.abs(true - pred)*weight) / tf.to_float(tf.size(pred))

def gradient_x(img):
    gx = img[:,:,:-1,:] - img[:,:,1:,:]
    return gx

def gradient_y(img):
    gy = img[:,:-1,:,:] - img[:,1:,:,:]
    return gy

def cal_grad_error(flo, image, beta):
  """Calculate the gradient of the given image by calculate the difference between nearby pixels
  """
  error = 0.0
  img_grad_x = gradient_x(image)
  img_grad_y = gradient_y(image)
  
  weights_x = tf.exp(-10.0*tf.reduce_mean(tf.abs(img_grad_x), 3, keep_dims=True))
  weights_y = tf.exp(-10.0*tf.reduce_mean(tf.abs(img_grad_y), 3, keep_dims=True))
  
  error += mean_charb_error_wmask(flo[:, 1:, :, :], flo[:, :-1, :, :], weights_y, beta)
  error += mean_charb_error_wmask(flo[:, :, 1:, :], flo[:, :, :-1, :], weights_x, beta)
    
  return error / 2.0

def img_grad_error(true, pred, mask, beta):
  error = 0.0
  
  error += mean_charb_error_wmask(true[:, 1:, :, :] - true[:, :-1, :, :], 
                            pred[:, 1:, :, :] - pred[:, :-1, :, :], mask[:, 1:, :, :], beta)
  error += mean_charb_error_wmask(true[:, :, 1:, :] - true[:, :, :-1, :], 
                            pred[:, :, 1:, :] - pred[:, :, :-1, :], mask[:, :, 1:, :], beta)
  
  return error / 2.0
  

def cal_epe(flo1, flo2):
  return tf.reduce_mean(tf.sqrt(tf.reduce_sum(tf.square(flo1 - flo2), axis=3)))

def blur(image):
  batch_size, img_height, img_width, color_channels = map(int, image.get_shape()[0:4])
  kernel = np.array([1., 2., 1., 2., 4., 2., 1., 2., 1.], dtype=np.float32) / 16.0
  kernel = kernel.reshape((3, 3, 1, 1))
  kernel = tf.constant(kernel, shape=(3, 3, 1, 1), 
                       name='gaussian_kernel', verify_shape=True)
  
  blur_image = tf.nn.depthwise_conv2d(image, tf.tile(kernel, [1, 1, color_channels, 1]), 
                                           [1, 1, 1, 1], 'SAME')
  return blur_image

def down_sample(image):
  batch_size, img_height, img_width, color_channels = map(int, image.get_shape()[0:4])
  blur_image = blur(image)
  return tf.image.resize_bicubic(blur_image, [img_height/2, img_width/2])
  
def get_pyrimad(image):
  batch_size, img_height, img_width, color_channels = map(int, image.get_shape()[0:4])
  
#   image1 = down_sample(image)
#   image2 = down_sample(image1)
#   image3 = down_sample(image2)
#   image4 = down_sample(image3)

  image1 = tf.image.resize_area(image, [img_height/2, img_width/2])
  image2 = tf.image.resize_area(image, [img_height/4, img_width/4])
  image3 = tf.image.resize_area(image, [img_height/8, img_width/8])
  image4 = tf.image.resize_area(image, [img_height/16, img_width/16])

  return image, image1, image2, image3, image4
  
def get_channel(image):
  zeros = tf.zeros_like(image)
  ones = tf.ones_like(image)
  
  #gray = 0.21*image[:, :, :, 0] + 0.72*image[:, :, :, 1] + 0.07*image[:, :, :, 2]
  channels = []
  for i in range(10):
    channels.append(tf.where(tf.logical_and(image >= i/10.0, image < (i+1)/10.0), ones, zeros))
  
  return tf.concat([image]+channels, axis=3)

def average_gradients(tower_grads):
  """Calculate the average gradient for each shared variable across all towers.

  Note that this function provides a synchronization point across all towers.

  Args:
    tower_grads: List of lists of (gradient, variable) tuples. The outer list
      is over individual gradients. The inner list is over the gradient
      calculation for each tower.
  Returns:
     List of pairs of (gradient, variable) where the gradient has been averaged
     across all towers.
  """
  average_grads = []
  for grad_and_vars in zip(*tower_grads):
    # Note that each grad_and_vars looks like the following:
    #   ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
    grads = []
    for g, _ in grad_and_vars:
      # Add 0 dimension to the gradients to represent the tower.
      expanded_g = tf.expand_dims(g, 0)

      # Append on a 'tower' dimension which we will average over below.
      grads.append(expanded_g)

    # Average over the 'tower' dimension.
    grad = tf.concat(axis=0, values=grads)
    grad = tf.reduce_mean(grad, 0)

    # Keep in mind that the Variables are redundant because they are shared
    # across towers. So .. we will just return the first tower's pointer to
    # the Variable.
    v = grad_and_vars[0][1]
    grad_and_var = (grad, v)
    average_grads.append(grad_and_var)
  return average_grads

class Model(object):

  def __init__(self,
               image1=None,
               image2=None,
               true_flo=None,
               reuse_scope=False,
               scope=None,
               prefix="train"):

    #self.prefix = prefix = tf.placeholder(tf.string, [])
    self.iter_num = tf.placeholder(tf.float32, [])
    summaries = []
    
    image1_0, image1_1, image1_2, image1_3, image1_4 = get_pyrimad(image1)
    image2_0, image2_1, image2_2, image2_3, image2_4 = get_pyrimad(image2)
    
    batch_size, H, W, color_channels = map(int, image1.get_shape()[0:4])
    
    if not reuse_scope:
      flow4 = construct_model(image1_4, image2_4, tf.zeros([batch_size, H/(2**4), W/(2**4), 2]), level=4)
      flow4to3 = tf.image.resize_bilinear(flow4, [H/(2**3), W/(2**3)])
      flow3 = construct_model(image1_3, image2_3, flow4to3, level=3) + flow4to3
      flow3to2 = tf.image.resize_bilinear(flow3, [H/(2**2), W/(2**2)])
      flow2 = construct_model(image1_2, image2_2, flow3to2, level=2) + flow3to2
      flow2to1 = tf.image.resize_bilinear(flow2, [H/(2**1), W/(2**1)])
      flow1 = construct_model(image1_1, image2_1, flow2to1, level=1) + flow2to1
      
    else:  # If it's a validation or test model.
      with tf.variable_scope(scope, reuse=True):
        flow4 = construct_model(image1_4, image2_4, tf.zeros([batch_size, H/(2**4), W/(2**4), 2]), level=4)
        flow4to3 = tf.image.resize_bilinear(flow4, [H/(2**3), W/(2**3)])
        flow3 = construct_model(image1_3, image2_3, flow4to3, level=3) + flow4to3
        flow3to2 = tf.image.resize_bilinear(flow3, [H/(2**2), W/(2**2)])
        flow2 = construct_model(image1_2, image2_2, flow3to2, level=2) + flow3to2
        flow2to1 = tf.image.resize_bilinear(flow2, [H/(2**1), W/(2**1)])
        flow1 = construct_model(image1_1, image2_1, flow2to1, level=1) + flow2to1
        
    with tf.variable_scope(scope, reuse=True):
      flow4r = construct_model(image2_4, image1_4, tf.zeros([batch_size, H/(2**4), W/(2**4), 2]), level=4)
      flow4to3r = tf.image.resize_bilinear(flow4r, [H/(2**3), W/(2**3)])
      flow3r = construct_model(image2_3, image1_3, flow4to3r, level=3) + flow4to3r
      flow3to2r = tf.image.resize_bilinear(flow3r, [H/(2**2), W/(2**2)])
      flow2r = construct_model(image2_2, image1_2, flow3to2r, level=2) + flow3to2r
      flow2to1r = tf.image.resize_bilinear(flow2r, [H/(2**1), W/(2**1)])
      flow1r = construct_model(image2_1, image1_1, flow2to1r, level=1) + flow2to1r
    
    
    occu_mask_4 = tf.clip_by_value(transformerFwd(tf.ones(shape=[batch_size, H/16, W/16, 1], dtype='float32'), 
                                 20*flow4r/16.0, [H/16, W/16]),
                                   clip_value_min=0.0, clip_value_max=1.0)
    occu_mask_3 = tf.clip_by_value(transformerFwd(tf.ones(shape=[batch_size, H/8, W/8, 1], dtype='float32'), 
                                 20*flow3r/8.0, [H/8, W/8]),
                                   clip_value_min=0.0, clip_value_max=1.0)
    occu_mask_2 = tf.clip_by_value(transformerFwd(tf.ones(shape=[batch_size, H/4, W/4, 1], dtype='float32'), 
                                 20*flow2r/4.0, [H/4, W/4]),
                                   clip_value_min=0.0, clip_value_max=1.0)
    occu_mask_1 = tf.clip_by_value(transformerFwd(tf.ones(shape=[batch_size, H/2, W/2, 1], dtype='float32'), 
                                 20*flow1r/2.0, [H/2, W/2]),
                                   clip_value_min=0.0, clip_value_max=1.0)
    
    image1_4p = transformer(image2_4, 20*flow4/(2**4), [H/(2**4), W/(2**4)])
    loss4 = mean_charb_error_wmask(image1_4, image1_4p, occu_mask_4, 1.0)
    image1_3p = transformer(image2_3, 20*flow3/(2**3), [H/(2**3), W/(2**3)])
    loss3 = mean_charb_error_wmask(image1_3, image1_3p, occu_mask_3, 1.0)
    image1_2p = transformer(image2_2, 20*flow2/(2**2), [H/(2**2), W/(2**2)])
    loss2 = mean_charb_error_wmask(image1_2, image1_2p, occu_mask_2, 1.0)
    image1_1p = transformer(image2_1, 20*flow1/(2**1), [H/(2**1), W/(2**1)])
    loss1 = mean_charb_error_wmask(image1_1, image1_1p, occu_mask_1, 1.0)

    img_grad_error4 = img_grad_error(image1_4p, image1_4, occu_mask_4, 1.0)
    img_grad_error3 = img_grad_error(image1_3p, image1_3, occu_mask_3, 1.0)
    img_grad_error2 = img_grad_error(image1_2p, image1_2, occu_mask_2, 1.0)
    img_grad_error1 = img_grad_error(image1_1p, image1_1, occu_mask_1, 1.0)
         
    grad_error4 = cal_grad_error(flow4, image1_4, 1.0/(2**4))
    grad_error3 = cal_grad_error(flow3, image1_3, 1.0/(2**3))
    grad_error2 = cal_grad_error(flow2, image1_2, 1.0/(2**2))
    grad_error1 = cal_grad_error(flow1, image1_1, 1.0/(2**1))
     
#     loss = 0.05*(loss2+img_grad_error2) + 0.1*(loss3+img_grad_error3) + \
#            0.2*(loss4+img_grad_error4) + 0.8*(loss5+img_grad_error5) + 3.2*(loss6+img_grad_error6) + \
#            (0.05*grad_error2 + 0.1*grad_error3 + 0.2*grad_error4 + 0.8*grad_error5 + 3.2*grad_error6)*2.0
 
    loss = 1.0*(loss4+img_grad_error4) + 1.0*(loss3+img_grad_error3) + 1.0*(loss2+img_grad_error2) + \
           1.0*(loss1+img_grad_error1) + \
           (1.0*grad_error4 + 1.0*grad_error3 + 1.0*grad_error2 + 1.0*grad_error1)*10.0
#    loss = 3.2*(loss2+img_grad_error2) + 0.8*(loss3+img_grad_error3) + \
#           0.2*(loss4+img_grad_error4) + 0.1*(loss5+img_grad_error5) + 0.05*(loss6+img_grad_error6) + \
#           (3.2*grad_error2 + 0.8*grad_error3 + 0.2*grad_error4 + 0.1*grad_error5 + 0.05*grad_error6)*10.0
         
    self.loss = loss
    self.orig_image1 = image1_1
    self.orig_image2 = image2_1
    self.true_flo = tf.image.resize_bicubic(true_flo/(2**1), [H/(2**1), W/(2**1)]) 
    self.pred_flo = 20*flow1 / (2**1)
    self.true_warp = transformer(self.orig_image2, self.true_flo, [H/2, W/2])
    self.pred_warp = image1_1p    
     
    summaries.append(tf.summary.scalar(prefix + '_loss', self.loss))
    summaries.append(tf.summary.scalar(prefix + '_loss1', loss1))
    summaries.append(tf.summary.scalar(prefix + '_loss2', loss2))
    summaries.append(tf.summary.scalar(prefix + '_loss3', loss3))
    summaries.append(tf.summary.scalar(prefix + '_loss4', loss4))
    summaries.append(tf.summary.scalar(prefix + '_grad_loss1', grad_error1))
    summaries.append(tf.summary.scalar(prefix + '_grad_loss2', grad_error2))
    summaries.append(tf.summary.scalar(prefix + '_grad_loss3', grad_error3))
    summaries.append(tf.summary.scalar(prefix + '_grad_loss4', grad_error4))
    summaries.append(tf.summary.scalar(prefix + '_flo_loss', cal_epe(self.true_flo, self.pred_flo)))
    self.summ_op = tf.summary.merge(summaries)

class Model_eval(object):

  def __init__(self,
               image1=None,
               image2=None,
               true_flo=None,
               scope=None,
               prefix="eval"):

    #self.prefix = prefix = tf.placeholder(tf.string, [])
    self.iter_num = tf.placeholder(tf.float32, [])
    summaries = []
    
    image1_0, image1_1, image1_2, image1_3, image1_4 = get_pyrimad(image1)
    image2_0, image2_1, image2_2, image2_3, image2_4 = get_pyrimad(image2)
    
    batch_size, H, W, color_channels = map(int, image1.get_shape()[0:4])
    
    with tf.variable_scope(scope, reuse=True):
      flow4 = construct_model(image1_4, image2_4, tf.zeros([batch_size, H/(2**4), W/(2**4), 2]), level=4)
      flow4to3 = tf.image.resize_bilinear(flow4, [H/(2**3), W/(2**3)])
      flow3 = construct_model(image1_3, image2_3, flow4to3, level=3) + flow4to3
      flow3to2 = tf.image.resize_bilinear(flow3, [H/(2**2), W/(2**2)])
      flow2 = construct_model(image1_2, image2_2, flow3to2, level=2) + flow3to2
      flow2to1 = tf.image.resize_bilinear(flow2, [H/(2**1), W/(2**1)])
      flow1 = construct_model(image1_1, image2_1, flow2to1, level=1) + flow2to1
     
    image1_1p = transformer(image2_1, 20*flow1/(2**1), [H/(2**1), W/(2**1)])
 
    self.orig_image1 = image1_1
    self.orig_image2 = image2_1
    self.true_flo = tf.image.resize_bicubic(true_flo/(2**1), [H/(2**1), W/(2**1)])
    self.pred_flo = 20*flow1 / (2**1)
    self.true_warp = transformer(self.orig_image2, self.true_flo, [H/2, W/2])
    self.pred_warp = image1_1p   
    
    self.epe = cal_epe(true_flo, tf.image.resize_bicubic(20*flow1, [H, W]))
    self.epe1 = cal_epe(tf.image.resize_bicubic(true_flo/(2**1), [H/(2**1), W/(2**1)]), 
                        20*flow1 / (2**1))
    self.epe2 = cal_epe(tf.image.resize_bicubic(true_flo/(2**2), [H/(2**2), W/(2**2)]), 
                        20*flow2 / (2**2))
    self.epe3 = cal_epe(tf.image.resize_bicubic(true_flo/(2**3), [H/(2**3), W/(2**3)]), 
                        20*flow3 / (2**3))
    self.epe4 = cal_epe(tf.image.resize_bicubic(true_flo/(2**4), [H/(2**4), W/(2**4)]), 
                        20*flow4 / (2**4))
    
    summaries.append(tf.summary.scalar(prefix + '_flo_loss', self.epe))
    summaries.append(tf.summary.scalar(prefix + '_flo_loss1', self.epe1))
    summaries.append(tf.summary.scalar(prefix + '_flo_loss2', self.epe2))
    summaries.append(tf.summary.scalar(prefix + '_flo_loss3', self.epe3))
    summaries.append(tf.summary.scalar(prefix + '_flo_loss4', self.epe4))
    self.summ_op = tf.summary.merge(summaries)

def plot_all(model, itr, sess, feed_dict):
  orig_image1, true_flo, pred_flo, true_warp, pred_warp = sess.run([model.orig_image1, 
                                              model.true_flo, 
                                              model.pred_flo,
                                              model.true_warp,
                                              model.pred_warp],
                                             feed_dict)
  
  plot_general([orig_image1, true_warp, pred_warp, true_flo, pred_flo], h=2, w=3, output_dir=FLAGS.output_dir, itr=itr)

def main(unused_argv):
  if FLAGS.output_dir == "":
    raise Exception("OUT_DIR must be specified")
  
  if os.path.exists(FLAGS.output_dir):
    raise Exception("OUT_DIR already exist")
    
  print 'Constructing models and inputs.'
  
  with tf.Graph().as_default(), tf.device('/cpu:0'):
    train_op = tf.train.AdamOptimizer(FLAGS.learning_rate)
    
    tower_grads = []
    itr_placeholders = []
    
    image1, image2, flo= build_tfrecord_input(training=True)
    
    split_image1 = tf.split(axis=0, num_or_size_splits=FLAGS.num_gpus, value=image1)
    split_image2 = tf.split(axis=0, num_or_size_splits=FLAGS.num_gpus, value=image2)
    split_flo = tf.split(axis=0, num_or_size_splits=FLAGS.num_gpus, value=flo)
    
    eval_image1, eval_image2, eval_flo = build_tfrecord_input(training=False)
        
    summaries_cpu = tf.get_collection(tf.GraphKeys.SUMMARIES, tf.get_variable_scope().name)

    with tf.variable_scope(tf.get_variable_scope()) as vs:
      for i in xrange(FLAGS.num_gpus):
        with tf.device('/gpu:%d' % i):
          if i == FLAGS.num_gpus - 1:
            scopename = "model"
          else:
            scopename = '%s_%d' % ("tower", i)
          with tf.name_scope(scopename) as ns:
            if i == 0:
              model = Model(split_image1[i], split_image2[i], split_flo[i], reuse_scope=False, scope=vs)
            else:
              model = Model(split_image1[i], split_image2[i], split_flo[i], reuse_scope=True, scope=vs)
            
            loss = model.loss
            # Retain the summaries from the final tower.
            if i == FLAGS.num_gpus - 1:
              summaries = tf.get_collection(tf.GraphKeys.SUMMARIES, ns)
              eval_model = Model_eval(eval_image1, eval_image2, eval_flo, scope=vs)
            # Calculate the gradients for the batch of data on this CIFAR tower.
            grads = train_op.compute_gradients(loss)

            # Keep track of the gradients across all towers.
            tower_grads.append(grads)
            itr_placeholders.append(model.iter_num)
            
            
        # We must calculate the mean of each gradient. Note that this is the
    # synchronization point across all towers.
    grads = average_gradients(tower_grads)

    # Apply the gradients to adjust the shared variables.
    apply_gradient_op = train_op.apply_gradients(grads)

    # Create a saver.
    saver = tf.train.Saver(
        tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES), max_to_keep=5)
    
    # saver1 = tf.train.Saver(
    #     list(set(tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES))-set(tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=".*ae.*"))), max_to_keep=5)
    
    # saver2 = tf.train.Saver(
    #     tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=".*ae.*"), max_to_keep=5)

    # Build the summary operation from the last tower summaries.
    summary_op = tf.summary.merge(summaries + summaries_cpu)

    # Make training session.
    sess = tf.Session(config=tf.ConfigProto(
        allow_soft_placement=True,
        log_device_placement=False))
    summary_writer = tf.summary.FileWriter(
        FLAGS.output_dir, graph=sess.graph, flush_secs=10)
  
    if FLAGS.pretrained_model:
      saver.restore(sess, FLAGS.pretrained_model)
      #saver2.restore(sess, "./tmp/flow_exp/flow_learn_chair_copy_ae_bal/model65002")
      #start_itr = int(FLAGS.pretrained_model.split("/")[-1][5:])
      start_itr = 0
      sess.run(tf.local_variables_initializer())
    else:
      sess.run(tf.global_variables_initializer())
      sess.run(tf.local_variables_initializer())
      start_itr = 0
      
    tf.train.start_queue_runners(sess)
    
    average_epe = tf.placeholder(tf.float32)
    average_epe_summary = tf.summary.scalar("model/eval_average_epe", average_epe)
    epes = []
    
    average_epe1 = tf.placeholder(tf.float32)
    average_epe1_summary = tf.summary.scalar("model/eval_average_epe1", average_epe1)
    epes1 = []
    
    average_epe2 = tf.placeholder(tf.float32)
    average_epe2_summary = tf.summary.scalar("model/eval_average_epe2", average_epe2)
    epes2 = []
    
    average_epe3 = tf.placeholder(tf.float32)
    average_epe3_summary = tf.summary.scalar("model/eval_average_epe3", average_epe3)
    epes3 = []
    
    average_epe4 = tf.placeholder(tf.float32)
    average_epe4_summary = tf.summary.scalar("model/eval_average_epe4", average_epe4)
    epes4 = []
    
    # Run training.
    for itr in range(start_itr, FLAGS.num_iterations):
      # Generate new batch of data.
      feed_dict = {x:np.float32(itr) for x in itr_placeholders}
      _, summary_str = sess.run([apply_gradient_op, summary_op],
                                      feed_dict)
      
      if (itr) % (SUMMARY_INTERVAL) == 2:
        summary_writer.add_summary(summary_str, itr)
      
      if (itr) % SAVE_INTERVAL == 2:
        if (itr) % (SAVE_INTERVAL*10) == 2:
          tf.logging.info('Saving model.')
          saver.save(sess, FLAGS.output_dir + '/model' + str(itr))
        
        plot_all(eval_model, itr, sess, feed_dict)
          
      if (itr) % (SUMMARY_INTERVAL) == 2:
        eval_summary_str, epe, epe1, epe2, epe3, epe4 = sess.run([eval_model.summ_op, eval_model.epe, 
                                                eval_model.epe1,
                                                eval_model.epe2,
                                                eval_model.epe3,
                                                eval_model.epe4])
        epes.append(epe)
        epes1.append(epe1)
        epes2.append(epe2)
        epes3.append(epe3)
        epes4.append(epe4)
        
        if len(epes) == 386:
          epes.pop(0)
          epes1.pop(0)
          epes2.pop(0)
          epes3.pop(0)
          epes4.pop(0)
        feed = {average_epe: sum(epes)/len(epes), 
                average_epe1: sum(epes1)/len(epes1),
                average_epe2: sum(epes2)/len(epes2),
                average_epe3: sum(epes3)/len(epes3),
                average_epe4: sum(epes4)/len(epes4)}
        epe_summary_str, epe1_summary_str, epe2_summary_str, epe3_summary_str, epe4_summary_str = sess.run([average_epe_summary, 
                                                      average_epe1_summary,
                                                      average_epe2_summary,
                                                      average_epe3_summary,
                                                      average_epe4_summary], feed_dict=feed)
        
        summary_writer.add_summary(eval_summary_str, itr)
        summary_writer.add_summary(epe_summary_str, itr)
        summary_writer.add_summary(epe1_summary_str, itr)
        summary_writer.add_summary(epe2_summary_str, itr)
        summary_writer.add_summary(epe3_summary_str, itr)
        summary_writer.add_summary(epe4_summary_str, itr)
  

if __name__ == '__main__':
  app.run()
