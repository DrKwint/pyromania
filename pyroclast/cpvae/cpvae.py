import os
import os.path as osp
from pathlib import Path
import json

import joblib
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
from PIL import Image
from tqdm import tqdm

from pyroclast.common.early_stopping import EarlyStopping
from pyroclast.cpvae.ddt import DDT
from pyroclast.cpvae.util import build_saveable_objects, calculate_walk

tfd = tfp.distributions

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'


def setup(data_dict,
          optimizer,
          encoder,
          decoder,
          learning_rate,
          latent_dim,
          prior_name,
          posterior_name,
          output_distribution_name,
          max_tree_depth,
          output_dir,
          oversample,
          debug=False,
          expect_load=False):
    num_channels = data_dict['shape'][-1]

    # setup model vars
    model_dir = osp.join(output_dir, 'model')
    objects = build_saveable_objects(
        optimizer_name=optimizer,
        encoder_name=encoder,
        decoder_name=decoder,
        learning_rate=learning_rate,
        num_channels=num_channels,
        latent_dim=latent_dim,
        prior_name=prior_name,
        posterior_name=posterior_name,
        output_distribution_name=output_distribution_name,
        max_tree_depth=max_tree_depth,
        model_dir=model_dir,
        model_name=encoder + decoder)

    model = objects['model']
    optimizer = objects['optimizer']
    global_step = objects['global_step']
    checkpoint = objects['checkpoint']
    ckpt_manager = objects['ckpt_manager']
    classifier = objects['classifier']
    writer = tf.summary.create_file_writer(output_dir)

    # load trained model, if available
    if tf.train.latest_checkpoint(model_dir):
        checkpoint.restore(tf.train.latest_checkpoint(model_dir))
        print("loaded a model from disk at",
              tf.train.latest_checkpoint(model_dir))
    elif expect_load:
        raise Exception("Model not loaded")

    # train a ddt
    if tf.train.latest_checkpoint(model_dir):
        print("loaded decision tree")
        model.classifier = joblib.load(osp.join(output_dir, 'ddt.joblib'))
    else:
        classifier.update_model_tree(data_dict['train'],
                                     model.posterior,
                                     oversample=oversample,
                                     debug=debug)
        classifier.save_dot(output_dir, 'initial')
        joblib.dump(classifier, osp.join(output_dir, 'ddt.joblib'))
    return model, optimizer, global_step, writer, checkpoint, ckpt_manager


def setup_from_dir(data_dict, seed, output_dir, fpath, debug=False, **kwargs):
    with open(fpath) as param_json:
        cmd_line_params = json.load(param_json)
    args = cmd_line_params['args']
    module_kwargs = cmd_line_params['module_kwargs']
    retrieve = lambda name: module_kwargs[name] if name in args.keys(
    ) else kwargs[name]
    optimizer = retrieve('optimizer')
    learning_rate = retrieve('learning_rate')
    encoder = retrieve('encoder')
    decoder = retrieve('decoder')
    latent_dim = retrieve('latent_dim')
    prior_name = retrieve('prior')
    posterior_name = retrieve('posterior')
    output_distribution_name = retrieve('output_distribution')
    max_tree_depth = retrieve('max_tree_depth')
    oversample = retrieve('oversample')

    outs = setup(data_dict,
                 optimizer,
                 encoder,
                 decoder,
                 learning_rate,
                 latent_dim,
                 prior_name,
                 posterior_name,
                 output_distribution_name,
                 max_tree_depth,
                 output_dir,
                 oversample,
                 debug=False,
                 expect_load=True)
    print(outs)


def outer_run_minibatch(model,
                        optimizer,
                        global_step,
                        alpha,
                        beta,
                        gamma,
                        writer,
                        clip_norm=0.):

    def run_minibatch(data, labels, is_train=True, prefix='train'):
        x = tf.cast(data, tf.float32) / 255.
        labels = tf.cast(labels, tf.int32)

        with tf.GradientTape() as tape:
            global_step.assign_add(1)
            z_posterior, leaf_probs, y_hat = model(x)
            y_hat = tf.cast(y_hat, tf.float32)  # from double to single fp

            distortion, rate = model.vae_loss(x,
                                              z_posterior,
                                              y=labels,
                                              training=is_train)
            classification_loss = tf.losses.sparse_categorical_crossentropy(
                y_true=labels, y_pred=y_hat)
            loss = tf.reduce_mean(alpha * distortion + beta * rate +
                                  gamma * classification_loss)

        # calculate gradients for current loss
        if is_train:
            gradients = tape.gradient(loss, model.trainable_variables)
            """
            tf.print(
                list(
                    zip([tf.reduce_mean(g) for g in gradients],
                        [v.name for v in model.trainable_variables])))
            """
            if clip_norm:
                clipped_gradients, _ = tf.clip_by_global_norm(
                    gradients, clip_norm)
            else:
                clipped_gradients = gradients
            optimizer.apply_gradients([
                (grad, var)
                for (grad,
                     var) in zip(clipped_gradients, model.trainable_variables)
                if grad is not None
            ])

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
            tf.summary.scalar(prefix + "leaf distribution entropy",
                              tf.reduce_mean(leaf_probs.entropy()),
                              step=global_step)
            tf.summary.scalar(prefix + "loss/total loss",
                              loss,
                              step=global_step)

        loss_numerator = tf.reduce_sum(alpha * distortion + beta * rate +
                                       gamma * classification_loss)
        classification_rate_numerator = tf.reduce_sum(
            tf.cast(tf.equal(prediction, labels), tf.float32))
        loss_denominator = x.shape[0]
        return loss_numerator, classification_rate_numerator, loss_denominator

    return run_minibatch


def sample(model, num_samples, epoch, output_dir):
    for i in range(num_samples):
        im = np.squeeze(model.sample_prior())
        im = np.minimum(1., np.maximum(0., im))
        im = Image.fromarray((255. * im).astype(np.uint8))
        im.save(
            os.path.join(output_dir, "epoch_{}_sample_{}.png".format(epoch, i)))


def train(data_dict, model, optimizer, global_step, writer, early_stopping,
          alpha, beta, gamma, omega, clip_norm, tree_update_period, num_samples,
          output_dir, oversample, debug):
    output_log_file = "file://" + osp.join(output_dir, 'train_log.txt')
    run_minibatch_fn = outer_run_minibatch(model, optimizer, global_step, alpha,
                                           beta, gamma, writer, clip_norm)
    run_minibatch_fn = tf.function(run_minibatch_fn)
    # run training loop
    train_batches = data_dict['train']
    if debug:
        train_batches = tqdm(train_batches, total=data_dict['train_bpe'])
    test_batches = data_dict['test']
    if debug:
        test_batches = tqdm(test_batches, total=data_dict['test_bpe'])
    for epoch in range(early_stopping.max_epochs):
        # train
        tf.print("Epoch", epoch)
        tf.print("Epoch", epoch, output_stream=output_log_file)
        tf.print("TRAIN", output_stream=output_log_file)
        loss_numerator = 0
        loss_denominator = 0
        classification_rate_numerator = 0
        for batch in train_batches:
            loss_n, class_rate_n, loss_d = run_minibatch_fn(
                data=batch['image'],
                labels=batch['label'],
                is_train=tf.constant(True),
                prefix='train/')
            loss_numerator += loss_n
            classification_rate_numerator += class_rate_n
            loss_denominator += loss_d
        tf.print("loss:",
                 float(loss_numerator) / float(loss_denominator),
                 output_stream=output_log_file)
        tf.print("classification_rate:",
                 float(classification_rate_numerator) / float(loss_denominator),
                 output_stream=output_log_file)

        # test
        loss_numerator = 0
        loss_denominator = 0
        classification_rate_numerator = 0
        tf.print("TEST", output_stream=output_log_file)
        for batch in test_batches:
            loss_n, class_rate_n, loss_d = run_minibatch_fn(
                data=batch['image'],
                labels=batch['label'],
                is_train=tf.constant(False),
                prefix='test/')
            loss_numerator += loss_n
            loss_denominator += loss_d
            classification_rate_numerator += class_rate_n
        tf.print("loss:",
                 float(loss_numerator) / float(loss_denominator),
                 output_stream=output_log_file)
        tf.print("classification_rate:",
                 float(classification_rate_numerator) / float(loss_denominator),
                 output_stream=output_log_file)

        # sample
        if debug:
            tf.print('Sampling')
        sample(model, num_samples, epoch, output_dir)

        # save parameters
        if early_stopping(epoch,
                          float(loss_numerator) / float(loss_denominator)):
            break

        # update
        if type(model.classifier) is DDT and epoch % tree_update_period == 0:
            if debug:
                tf.print('Updating decision tree')
            score = model.classifier.update_model_tree(data_dict['train'],
                                                       model.posterior,
                                                       oversample=oversample,
                                                       debug=debug)
            tf.print("Accuracy at DDT fit from sampling:",
                     score,
                     output_stream=output_log_file)
            model.classifier.save_dot(output_dir, epoch)
            joblib.dump(model.classifier, osp.join(output_dir, 'ddt.joblib'))
            model.prior = model.classifier.tree_distribution

    return model


def learn(
    data_dict,
    encoder,
    decoder,
    seed,
    latent_dim,
    epochs=1000,
    oversample=1,
    max_tree_depth=5,
    tree_update_period=3,
    optimizer='rmsprop',  # adam or rmsprop
    learning_rate=3e-4,
    prior='iso_gaussian_prior',
    posterior='diag_gaussian_posterior',
    output_distribution='disc_logistic_posterior',  # disc_logistic or l2 or bernoulli
    output_dir='./',
    num_samples=5,
    clip_norm=0.,
    alpha=1.,
    beta=1.,
    gamma=1.,
    omega=1.,
    patience=12,
    debug=False):
    model, optimizer, global_step, writer, checkpoint, ckpt_manager = setup(
        data_dict,
        optimizer,
        encoder,
        decoder,
        learning_rate,
        latent_dim,
        prior,
        posterior,
        output_distribution,
        max_tree_depth,
        output_dir=output_dir,
        oversample=oversample,
        debug=debug)

    early_stopping = EarlyStopping(patience,
                                   ckpt_manager,
                                   eps=0.03,
                                   max_epochs=epochs)
    model = train(data_dict, model, optimizer, global_step, writer,
                  early_stopping, alpha, beta, gamma, omega, clip_norm,
                  tree_update_period, num_samples, output_dir, oversample,
                  debug)
    return model


def walk(
    data_dict,
    encoder,
    decoder,
    seed=None,
    latent_dim=64,
    epochs=1000,
    oversample=1,
    max_tree_depth=5,
    tree_update_period=3,
    optimizer='rmsprop',  # adam or rmsprop
    learning_rate=3e-4,
    prior='iso_gaussian_prior',
    posterior='diag_gaussian_posterior',
    output_distribution='disc_logistic_posterior',  # disc_logistic or l2 or bernoulli
    output_dir='./',
    num_samples=5,
    clip_norm=0.,
    alpha=1.,
    beta=1.,
    gamma=1.,
    omega=1.,
    patience=12,
    debug=False):
    model, optimizer, global_step, writer, checkpoint, ckpt_manager = setup(
        data_dict,
        optimizer,
        encoder,
        decoder,
        learning_rate,
        latent_dim,
        prior,
        posterior,
        output_distribution,
        max_tree_depth,
        output_dir=output_dir,
        oversample=oversample,
        debug=debug)
    # axis of discrimination walks
    all_features = model.classifier.decision_tree.tree_.feature
    features = [
        all_features[i]
        for i in range(len(all_features) - 2)
        if all_features[i + 1] < 0 and all_features[i] > 0
    ]
    for i in range(len(model.classifier.tree_distribution.components) // 2):
        comp_a = model.classifier.tree_distribution.components[2 * i]
        comp_b = model.classifier.tree_distribution.components[2 * i + 1]
        feature = features[i]
        forward_points, backward_points = calculate_walk(comp_a.loc,
                                                         comp_b.loc,
                                                         dim=feature)
        Path(os.path.join(output_dir, "axis_walks",
                          "leaf_pair_{}".format(i))).mkdir(parents=True,
                                                           exist_ok=True)
        for k, img in enumerate(model.decode(forward_points)):
            im = np.minimum(1., np.maximum(0, np.squeeze(img)))
            im = Image.fromarray((255. * im).astype(np.uint8))
            im.save(
                os.path.join(output_dir, "axis_walks", "leaf_pair_{}".format(i),
                             "forward_img_{}.png".format(k)))
        for k, img in enumerate(model.decode(backward_points)):
            im = np.minimum(1., np.maximum(0, np.squeeze(img)))
            im = Image.fromarray((255. * im).astype(np.uint8))
            im.save(
                os.path.join(output_dir, "axis_walks", "leaf_pair_{}".format(i),
                             "backward_img_{}.png".format(k)))

    # direct walks
    for i, component in enumerate(
            model.classifier.tree_distribution.components):
        for j, other in enumerate(
                model.classifier.tree_distribution.components):
            points = calculate_walk(component.loc, other.loc)
            Path(
                os.path.join(output_dir, "direct_walks",
                             "leaf_walk_{}_{}".format(i,
                                                      j))).mkdir(parents=True,
                                                                 exist_ok=True)
            for k, img in enumerate(model.decode(points)):
                im = np.minimum(1., np.maximum(0, np.squeeze(img)))
                im = Image.fromarray((255. * im).astype(np.uint8))
                im.save(
                    os.path.join(output_dir, "direct_walks",
                                 "leaf_walk_{}_{}".format(i, j),
                                 "img_{}.png".format(k)))
