#!/usr/bin/env python3
import argparse
import datetime
import os
import re

import numpy as np
import torch
import torchmetrics

from morpho_analyzer import MorphoAnalyzer
from morpho_dataset import MorphoDataset

# TODO: Define reasonable defaults and optionally more parameters.
# Also, you can set the number of threads to 0 to use all your CPU cores.
parser = argparse.ArgumentParser()
parser.add_argument("--batch_size", default=..., type=int, help="Batch size.")
parser.add_argument("--epochs", default=..., type=int, help="Number of epochs.")
parser.add_argument("--seed", default=42, type=int, help="Random seed.")
parser.add_argument("--threads", default=1, type=int, help="Maximum number of threads to use.")


class TrainableModule(torch.nn.Module):
    """A simple Keras-like module for training with raw PyTorch.

    The module provides fit/evaluate/predict methods, computes loss and metrics,
    and generates both TensorBoard and console logs. By default, it uses GPU
    if available, and CPU otherwise. Additionally, it offers a Keras-like
    initialization of the weights.

    The current implementation supports models with either single input or
    a tuple of inputs; however, only one output is currently supported.
    """
    from torch.utils.tensorboard import SummaryWriter as _SummaryWriter
    from time import time as _time
    from tqdm import tqdm as _tqdm

    def configure(self, *, optimizer=None, schedule=None, loss=None, metrics={}, logdir=None, device="auto"):
        """Configure the module process.

        - `optimizer` is the optimizer to use for training;
        - `schedule` is an optional learning rate scheduler used after every batch;
        - `loss` is the loss function to minimize;
        - `metrics` is a dictionary of additional metrics to compute;
        - `logdir` is an optional directory where TensorBoard logs should be written;
        - `device` is the device to use; when "auto", `cuda` is used when available, `cpu` otherwise.
        """
        self.optimizer = optimizer
        self.schedule = schedule
        self.loss, self.loss_metric = loss, torchmetrics.MeanMetric()
        self.metrics = torchmetrics.MetricCollection(metrics)
        self.logdir, self._writers = logdir, {}
        self.device = torch.device(("cuda" if torch.cuda.is_available() else "cpu") if device == "auto" else device)
        self.to(self.device)

    def load_weights(self, path, device="auto"):
        """Load the model weights from the given path."""
        self.device = torch.device(("cuda" if torch.cuda.is_available() else "cpu") if device == "auto" else device)
        self.load_state_dict(torch.load(path, map_location=self.device))

    def save_weights(self, path):
        """Save the model weights to the given path."""
        state_dict = self.state_dict()
        torch.save(state_dict, path)

    def fit(self, dataloader, epochs, dev=None, callbacks=[], verbose=1):
        """Train the model on the given dataset.

        - `dataloader` is the training dataset, each element a pair of inputs and an output;
          the inputs can be either a single tensor or a tuple of tensors;
        - `dev` is an optional development dataset;
        - `epochs` is the number of epochs to train;
        - `callbacks` is a list of callbacks to call after each epoch with
          arguments `self`, `epoch`, and `logs`;
        - `verbose` controls the verbosity: 0 for silent, 1 for persistent progress bar,
          2 for a progress bar only when writing to a console.
        """
        for epoch in range(epochs):
            self.train()
            self.loss_metric.reset()
            self.metrics.reset()
            start = self._time()
            epoch_message = f"Epoch={epoch+1}/{epochs}"
            data_and_progress = self._tqdm(
                dataloader, epoch_message, unit="batch", leave=False, disable=None if verbose == 2 else not verbose)
            for xs, y in data_and_progress:
                xs, y = tuple(x.to(self.device) for x in (xs if isinstance(xs, tuple) else (xs,))), y.to(self.device)
                logs = self.train_step(xs, y)
                message = [epoch_message] + [f"{k}={v:.{0<abs(v)<2e-4 and '3g' or '4f'}}" for k, v in logs.items()]
                data_and_progress.set_description(" ".join(message), refresh=False)
            if dev is not None:
                logs |= {"dev_" + k: v for k, v in self.evaluate(dev, verbose=0).items()}
            for callback in callbacks:
                callback(self, epoch, logs)
            self.add_logs("train", {k: v for k, v in logs.items() if not k.startswith("dev_")}, epoch + 1)
            self.add_logs("dev", {k[4:]: v for k, v in logs.items() if k.startswith("dev_")}, epoch + 1)
            verbose and print(epoch_message, "{:.1f}s".format(self._time() - start),
                              *[f"{k}={v:.{0<abs(v)<2e-4 and '3g' or '4f'}}" for k, v in logs.items()])
        return logs

    def train_step(self, xs, y):
        """An overridable method performing a single training step.

        A dictionary with the loss and metrics should be returned."""
        self.zero_grad()
        y_pred = self.forward(*xs)
        loss = self.loss(y_pred, y)
        loss.backward()
        with torch.no_grad():
            self.optimizer.step()
            self.schedule is not None and self.schedule.step()
            self.loss_metric.update(loss)
            self.metrics.update(y_pred, y)
            return {"loss": self.loss_metric.compute()} \
                | ({"lr": self.schedule.get_last_lr()[0]} if self.schedule else {}) \
                | self.metrics.compute()

    def evaluate(self, dataloader, verbose=1):
        """An evaluation of the model on the given dataset.

        - `dataloader` is the dataset to evaluate on, each element a pair of inputs
          and an output, the inputs either a single tensor or a tuple of tensors;
        - `verbose` controls the verbosity: 0 for silent, 1 for a single message."""
        self.eval()
        self.loss_metric.reset()
        self.metrics.reset()
        for xs, y in dataloader:
            xs, y = tuple(x.to(self.device) for x in (xs if isinstance(xs, tuple) else (xs,))), y.to(self.device)
            logs = self.test_step(xs, y)
        verbose and print("Evaluation", *[f"{k}={v:.{0<abs(v)<2e-4 and '3g' or '4f'}}" for k, v in logs.items()])
        return logs

    def test_step(self, xs, y):
        """An overridable method performing a single evaluation step.

        A dictionary with the loss and metrics should be returned."""
        with torch.no_grad():
            y_pred = self.forward(*xs)
            self.loss_metric.update(self.loss(y_pred, y))
            self.metrics.update(y_pred, y)
            return {"loss": self.loss_metric.compute()} | self.metrics.compute()

    def predict(self, dataloader, as_numpy=True):
        """Compute predictions for the given dataset.

        - `dataloader` is the dataset to predict on, each element either
          directly the input or a tuple whose first element is the input;
          the input can be either a single tensor or a tuple of tensors;
        - `as_numpy` is a flag controlling whether the output should be
          converted to a numpy array or kept as a PyTorch tensor.

        The method returns a Python list whose elements are predictions
        of the individual examples. Note that if the input was padded, so
        will be the predictions, which will then need to be trimmed."""
        self.eval()
        predictions = []
        for batch in dataloader:
            xs = batch[0] if isinstance(batch, tuple) else batch
            xs = tuple(x.to(self.device) for x in (xs if isinstance(xs, tuple) else (xs,)))
            batch = self.predict_step(xs)
            predictions.extend(batch.numpy(force=True) if as_numpy else batch)
        return predictions

    def predict_step(self, xs):
        """An overridable method performing a single prediction step."""
        with torch.no_grad():
            return self.forward(*xs)

    def writer(self, writer):
        """Possibly create and return a TensorBoard writer for the given name."""
        if writer not in self._writers:
            self._writers[writer] = self._SummaryWriter(os.path.join(self.logdir, writer))
        return self._writers[writer]

    def add_logs(self, writer, logs, step):
        """Log the given dictionary to TensorBoard with a given name and step number."""
        if logs and self.logdir:
            for key, value in logs.items():
                self.writer(writer).add_scalar(key, value, step)
            self.writer(writer).flush()

    @staticmethod
    def keras_init(module):
        """Initialize weights using the Keras defaults."""
        if isinstance(module, (torch.nn.Linear, torch.nn.Conv1d, torch.nn.Conv2d, torch.nn.Conv3d,
                               torch.nn.ConvTranspose1d, torch.nn.ConvTranspose2d, torch.nn.ConvTranspose3d)):
            torch.nn.init.xavier_uniform_(module.weight)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        if isinstance(module, (torch.nn.Embedding, torch.nn.EmbeddingBag)):
            torch.nn.init.uniform_(module.weight, -0.05, 0.05)
        if isinstance(module, (torch.nn.RNNBase, torch.nn.RNNCellBase)):
            for name, parameter in module.named_parameters():
                "weight_ih" in name and torch.nn.init.xavier_uniform_(parameter)
                "weight_hh" in name and torch.nn.init.orthogonal_(parameter)
                "bias" in name and torch.nn.init.zeros_(parameter)
                if "bias" in name and isinstance(module, (torch.nn.LSTM, torch.nn.LSTMCell)):
                    parameter.data[module.hidden_size:module.hidden_size * 2] = 1


def main(args: argparse.Namespace) -> None:
    # Set the random seed and the number of threads.
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.threads:
        torch.set_num_threads(args.threads)
        torch.set_num_interop_threads(args.threads)

    # Create logdir name
    args.logdir = os.path.join("logs", "{}-{}-{}".format(
        os.path.basename(globals().get("__file__", "notebook")),
        datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S"),
        ",".join(("{}={}".format(re.sub("(.)[^_]*_?", r"\1", k), v) for k, v in sorted(vars(args).items())))
    ))

    # Load the data. Using analyses is only optional.
    morpho = MorphoDataset("czech_pdt")
    analyses = MorphoAnalyzer("czech_pdt_analyses")

    # TODO: Create the model and train it
    model = ...

    # Generate test set annotations, but in `args.logdir` to allow parallel execution.
    os.makedirs(args.logdir, exist_ok=True)
    with open(os.path.join(args.logdir, "tagger_competition.txt"), "w", encoding="utf-8") as predictions_file:
        # TODO: Predict the tags on the test set; update the following code
        # if you use other output structure than in tagger_we.
        predictions = model.predict(...)

        for predicted_tags, forms in zip(predictions, morpho.test.forms.strings):
            for predicted_tag in np.argmax(predicted_tags[:, :len(forms)], axis=0):
                print(morpho.train.tags.word_vocab.string(predicted_tag), file=predictions_file)
            print(file=predictions_file)


if __name__ == "__main__":
    args = parser.parse_args([] if "__file__" not in globals() else None)
    main(args)
