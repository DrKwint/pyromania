import os

import numpy as np
import tensorflow as tf
from PIL import Image
from pyroclast.common.util import dummy_context_mgr
from pyroclast.cpvae.util import build_model
from pyroclast.cpvae.ddt import DDT
from tqdm import tqdm

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '1'


def setup(data_dict, optimizer, encoder, decoder, learning_rate, latent_dim,
          output_dist, max_tree_depth, max_tree_leaf_nodes, load_dir,
          output_dir):
    num_classes = data_dict['num_classes']
    num_channels = data_dict['shape'][-1]

    # setup model vars
    model, optimizer, global_step = build_model(
        optimizer_name=optimizer,
        encoder_name=encoder,
        decoder_name=decoder,
        learning_rate=learning_rate,
        num_classes=num_classes,
        num_channels=num_channels,
        latent_dim=latent_dim,
        output_dist=output_dist,
        max_tree_depth=max_tree_depth,
        max_tree_leaf_nodes=max_tree_leaf_nodes)

    #checkpointing and tensorboard
    writer = tf.summary.create_file_writer(output_dir)
    checkpoint = tf.train.Checkpoint(optimizer=optimizer,
                                     model=model,
                                     global_step=global_step)
    ckpt_manager = tf.train.CheckpointManager(checkpoint,
                                              directory=os.path.join(
                                                  output_dir, 'model'),
                                              max_to_keep=3,
                                              keep_checkpoint_every_n_hours=2)

    # load trained model, if available
    if load_dir:
        status = checkpoint.restore(tf.train.latest_checkpoint(str(load_dir)))
        print("load: ", status.assert_existing_objects_matched())

    # train a ddt
    model.classifier.update_model_tree(data_dict['train'], model.posterior)
    return model, optimizer, global_step, writer, ckpt_manager


def learn(
        data_dict,
        encoder,
        decoder,
        seed=None,
        latent_dim=64,
        epochs=1000,
        max_tree_depth=5,
        max_tree_leaf_nodes=16,
        tree_update_period=3,
        optimizer='rmsprop',  # adam or rmsprop
        learning_rate=3e-4,
        output_dist='l2',  # disc_logistic or l2 or bernoulli
        output_dir='./',
        load_dir=None,
        num_samples=5,
        clip_norm=0.,
        alpha=1.,
        beta=1.,
        gamma=1.,
        gamma_delay=0,
        debug=False):
    model, optimizer, global_step, writer, ckpt_manager = setup(
        data_dict, optimizer, encoder, decoder, learning_rate, latent_dim,
        output_dist, max_tree_depth, max_tree_leaf_nodes, load_dir, output_dir)

    # define minibatch fn
    def run_minibatch(epoch, batch, is_train=True):
        x = tf.cast(batch['image'], tf.float32) / 255.
        labels = tf.cast(batch['label'], tf.int32)

        with tf.GradientTape() if is_train else dummy_context_mgr() as tape:
            global_step.assign_add(1)
            x_hat_loc, y_hat, z_posterior, x_hat_scale = model(x)
            y_hat = tf.cast(y_hat, tf.float32)  # from double to single fp
            distortion, rate = model.vae_loss(x,
                                              x_hat_loc,
                                              x_hat_scale,
                                              z_posterior,
                                              y=labels)
            classification_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(
                labels=labels, logits=y_hat)
            classification_loss = classification_loss * float(
                epoch > gamma_delay)
            loss = tf.reduce_mean(alpha * distortion + beta * rate +
                                  gamma * classification_loss)

        # calculate gradients for current loss
        if is_train:
            gradients = tape.gradient(loss, model.trainable_variables)
            if clip_norm:
                clipped_gradients, pre_clip_global_norm = tf.clip_by_global_norm(
                    gradients, clip_norm)
            else:
                clipped_gradients = gradients
            optimizer.apply_gradients(
                zip(clipped_gradients, model.trainable_variables))

        prefix = 'train ' if is_train else 'validate '
        with writer.as_default():
            prediction = tf.math.argmax(y_hat, axis=1, output_type=tf.int32)
            classification_rate = tf.reduce_mean(
                tf.cast(tf.equal(prediction, labels), tf.float32))
            tf.summary.scalar(prefix + "loss/mean distortion",
                              alpha * tf.reduce_mean(distortion),
                              step=global_step)
            tf.summary.scalar(prefix + "loss/mean rate",
                              beta * tf.reduce_mean(rate),
                              step=global_step)
            tf.summary.scalar(prefix + "loss/mean classification loss",
                              gamma * tf.reduce_mean(classification_loss),
                              step=global_step)
            tf.summary.scalar(prefix + "classification_rate",
                              classification_rate,
                              step=global_step)
            tf.summary.scalar(prefix + "loss/total loss",
                              loss,
                              step=global_step)
            tf.summary.scalar(prefix + 'posterior/mean stddev',
                              tf.reduce_mean(z_posterior.stddev()),
                              step=global_step)
            tf.summary.scalar(prefix + 'posterior/min stddev',
                              tf.reduce_min(z_posterior.stddev()),
                              step=global_step)
            tf.summary.scalar(prefix + 'posterior/max stddev',
                              tf.reduce_max(z_posterior.stddev()),
                              step=global_step)

            if is_train:
                if debug:
                    for (v, g) in zip(model.trainable_variables, gradients):
                        if g is None:
                            continue
                        tf.summary.scalar(prefix +
                                          'gradient/mean of {}'.format(v.name),
                                          tf.reduce_mean(g),
                                          step=global_step)
                if clip_norm:
                    tf.summary.scalar("gradient/global norm",
                                      pre_clip_global_norm,
                                      step=global_step)

    # run training loop
    for epoch in range(epochs):
        # train
        train_batches = data_dict['train']
        if debug:
            print("Epoch", epoch)
            print("TRAIN")
            train_batches = tqdm(train_batches, total=data_dict['train_bpe'])
        for batch in train_batches:
            run_minibatch(epoch, batch, is_train=True)

        # test
        test_batches = data_dict['test']
        if debug:
            print("TEST")
            test_batches = tqdm(test_batches, total=data_dict['test_bpe'])
        for batch in test_batches:
            run_minibatch(epoch, batch, is_train=False)

        # save parameters
        if debug:
            print('Saving parameters')
        ckpt_manager.save(checkpoint_number=epoch)

        # update
        if type(model.classifier) is DDT and epoch % tree_update_period == 0:
            if debug:
                print('Updating decision tree')
            model.classifier.update_model_tree(data_dict['train'],
                                               model.posterior)

        # sample
        if debug:
            print('Sampling')
        for i in range(num_samples):
            im = np.squeeze(model.sample(use_class_prior=True)[0])
            im = np.minimum(1., np.maximum(0., im))
            im = Image.fromarray((255. * im).astype(np.uint8))
            im.save(
                os.path.join(output_dir,
                             "epoch_{}_sample_{}.png".format(epoch, i)))
