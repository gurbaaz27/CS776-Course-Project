from __future__ import absolute_import, division, print_function, unicode_literals

import logging
from typing import Optional, Union, TYPE_CHECKING

import numpy as np

from src.constants import FLOAT_NUMPY
from src.attacks.attack import AdversarialAttack
from src.classifiers.estimator import BaseEstimator, LossGradientsMixin
from src.classifiers.classifier import ClassifierMixin
from src.preprocessing.utils import (
    compute_success,
    get_labels_np_array,
    random_sphere,
    projection,
    check_and_transform_label_format,
)

if TYPE_CHECKING:
    from src.preprocessing.utils import CLASSIFIER_LOSS_GRADIENTS_TYPE

logger = logging.getLogger(__name__)


class FastGradientMethod(AdversarialAttack):

    _estimator_requirements = (BaseEstimator, LossGradientsMixin)

    def __init__(
        self,
        estimator: "CLASSIFIER_LOSS_GRADIENTS_TYPE",
        norm: Union[int, float, str] = np.inf,
        eps: Union[int, float, np.ndarray] = 0.3,
        eps_step: Union[int, float, np.ndarray] = 0.1,
        targeted: bool = False,
        num_random_init: int = 0,
        batch_size: int = 32,
        minimal: bool = False,
    ) -> None:
        super().__init__(estimator=estimator)
        self.norm = norm
        self.eps = eps
        self.eps_step = eps_step
        self._targeted = targeted
        self.num_random_init = num_random_init
        self.batch_size = batch_size
        self.minimal = minimal
        self._project = True

        self._batch_id = 0
        self._i_max_iter = 0

    def _check_compatibility_input_and_eps(self, x: np.ndarray):
        if isinstance(self.eps, np.ndarray):
            # Ensure the eps array is broadcastable
            if self.eps.ndim > x.ndim:  # pragma: no cover
                raise ValueError(
                    "The `eps` shape must be broadcastable to input shape."
                )

    def _minimal_perturbation(
        self, x: np.ndarray, y: np.ndarray, mask: np.ndarray
    ) -> np.ndarray:
        """
        Iteratively compute the minimal perturbation necessary to make the class prediction change. Stop when the
        first adversarial example was found.

        :param x: An array with the original inputs.
        :param y: Target values (class labels) one-hot-encoded of shape (nb_samples, nb_classes).
        :return: An array holding the adversarial examples.
        """
        adv_x = x.copy()

        # Compute perturbation with implicit batching
        for batch_id in range(int(np.ceil(adv_x.shape[0] / float(self.batch_size)))):
            batch_index_1, batch_index_2 = (
                batch_id * self.batch_size,
                (batch_id + 1) * self.batch_size,
            )
            batch = adv_x[batch_index_1:batch_index_2]
            batch_labels = y[batch_index_1:batch_index_2]

            mask_batch = mask
            if mask is not None:
                # Here we need to make a distinction: if the masks are different for each input, we need to index
                # those for the current batch. Otherwise (i.e. mask is meant to be broadcasted), keep it as it is.
                if len(mask.shape) == len(x.shape):
                    mask_batch = mask[batch_index_1:batch_index_2]

            # Get perturbation
            perturbation = self._compute_perturbation(batch, batch_labels, mask_batch)

            # Get current predictions
            active_indices = np.arange(len(batch))

            if isinstance(self.eps, np.ndarray) and isinstance(
                self.eps_step, np.ndarray
            ):
                if (
                    len(self.eps.shape) == len(x.shape)
                    and self.eps.shape[0] == x.shape[0]
                ):
                    current_eps = self.eps_step[batch_index_1:batch_index_2]
                    partial_stop_condition = (
                        current_eps <= self.eps[batch_index_1:batch_index_2]
                    ).all()

                else:
                    current_eps = self.eps_step
                    partial_stop_condition = (current_eps <= self.eps).all()

            else:
                current_eps = self.eps_step
                partial_stop_condition = current_eps <= self.eps

            while active_indices.size > 0 and partial_stop_condition:
                # Adversarial crafting
                current_x = self._apply_perturbation(
                    x[batch_index_1:batch_index_2], perturbation, current_eps
                )

                # Update
                batch[active_indices] = current_x[active_indices]
                adv_preds = self.estimator.predict(batch)

                # If targeted active check to see whether we have hit the target, otherwise head to anything but
                if self.targeted:
                    active_indices = np.where(
                        np.argmax(batch_labels, axis=1) != np.argmax(adv_preds, axis=1)
                    )[0]
                else:
                    active_indices = np.where(
                        np.argmax(batch_labels, axis=1) == np.argmax(adv_preds, axis=1)
                    )[0]

                # Update current eps and check the stop condition
                if isinstance(self.eps, np.ndarray) and isinstance(
                    self.eps_step, np.ndarray
                ):
                    if (
                        len(self.eps.shape) == len(x.shape)
                        and self.eps.shape[0] == x.shape[0]
                    ):
                        current_eps = (
                            current_eps + self.eps_step[batch_index_1:batch_index_2]
                        )
                        partial_stop_condition = (
                            current_eps <= self.eps[batch_index_1:batch_index_2]
                        ).all()

                    else:
                        current_eps = current_eps + self.eps_step
                        partial_stop_condition = (current_eps <= self.eps).all()

                else:
                    current_eps = current_eps + self.eps_step
                    partial_stop_condition = current_eps <= self.eps

            adv_x[batch_index_1:batch_index_2] = batch

        return adv_x

    def generate(
        self, x: np.ndarray, y: Optional[np.ndarray] = None, **kwargs
    ) -> np.ndarray:
        """Generate adversarial samples and return them in an array.

        :param x: An array with the original inputs.
        :param y: Target values (class labels) one-hot-encoded of shape (nb_samples, nb_classes) or indices of shape
                  (nb_samples,). Only provide this parameter if you'd like to use true labels when crafting adversarial
                  samples. Otherwise, model predictions are used as labels to avoid the "label leaking" effect
                  (explained in this paper: https://arxiv.org/abs/1611.01236). Default is `None`.
        :param mask: An array with a mask broadcastable to input `x` defining where to apply adversarial perturbations.
                     Shape needs to be broadcastable to the shape of x and can also be of the same shape as `x`. Any
                     features for which the mask is zero will not be adversarially perturbed.
        :type mask: `np.ndarray`
        :return: An array holding the adversarial examples.
        """
        mask = self._get_mask(x, **kwargs)

        # Ensure eps is broadcastable
        self._check_compatibility_input_and_eps(x=x)

        if isinstance(self.estimator, ClassifierMixin):
            if y is not None:
                y = check_and_transform_label_format(y, self.estimator.nb_classes)

            if y is None:
                # Throw error if attack is targeted, but no targets are provided
                if self.targeted:  # pragma: no cover
                    raise ValueError(
                        "Target labels `y` need to be provided for a targeted attack."
                    )

                # Use model predictions as correct outputs
                logger.info("Using model predictions as correct labels for FGM.")
                y_array = get_labels_np_array(self.estimator.predict(x, batch_size=self.batch_size))  # type: ignore
            else:
                y_array = y

            if self.estimator.nb_classes > 2:
                y_array = y_array / np.sum(y_array, axis=1, keepdims=True)

            # Return adversarial examples computed with minimal perturbation if option is active
            adv_x_best = x
            if self.minimal:
                logger.info("Performing minimal perturbation FGM.")
                adv_x_best = self._minimal_perturbation(x, y_array, mask)
                rate_best = 100 * compute_success(
                    self.estimator,  # type: ignore
                    x,
                    y_array,
                    adv_x_best,
                    self.targeted,
                    batch_size=self.batch_size,  # type: ignore
                )
            else:
                rate_best = 0.0
                for _ in range(max(1, self.num_random_init)):
                    adv_x = self._compute(
                        x,
                        x,
                        y_array,
                        mask,
                        self.eps,
                        self.eps,
                        self._project,
                        self.num_random_init > 0,
                    )

                    if self.num_random_init > 1:
                        rate = 100 * compute_success(
                            self.estimator,  # type: ignore
                            x,
                            y_array,
                            adv_x,
                            self.targeted,
                            batch_size=self.batch_size,  # type: ignore
                        )
                        if rate > rate_best:
                            rate_best = rate
                            adv_x_best = adv_x
                    else:
                        adv_x_best = adv_x

            logger.info(
                "Success rate of FGM attack: %.2f%%",
                rate_best
                if rate_best is not None
                else 100
                * compute_success(
                    self.estimator,  # type: ignore
                    x,
                    y_array,
                    adv_x_best,
                    self.targeted,
                    batch_size=self.batch_size,
                ),
            )

        else:
            if self.minimal:  # pragma: no cover
                raise ValueError(
                    "Minimal perturbation is only supported for classification."
                )

            if y is None:
                # Throw error if attack is targeted, but no targets are provided
                if self.targeted:  # pragma: no cover
                    raise ValueError(
                        "Target labels `y` need to be provided for a targeted attack."
                    )

                # Use model predictions as correct outputs
                logger.info("Using model predictions as correct labels for FGM.")
                y_array = self.estimator.predict(x, batch_size=self.batch_size)
            else:
                y_array = y

            adv_x_best = self._compute(
                x,
                x,
                y_array,
                None,
                self.eps,
                self.eps,
                self._project,
                self.num_random_init > 0,
            )

        return adv_x_best

    def _compute_perturbation(
        self, x: np.ndarray, y: np.ndarray, mask: Optional[np.ndarray]
    ) -> np.ndarray:
        # Pick a small scalar to avoid division by 0
        tol = 10e-8

        # Get gradient wrt loss; invert it if attack is targeted
        grad = self.estimator.loss_gradient(x, y) * (1 - 2 * int(self.targeted))

        # Write summary

        # Check for NaN before normalisation an replace with 0
        if grad.dtype != object and np.isnan(grad).any():  # pragma: no cover
            logger.warning(
                "Elements of the loss gradient are NaN and have been replaced with 0.0."
            )
            grad = np.where(np.isnan(grad), 0.0, grad)
        else:
            for i, _ in enumerate(grad):
                grad_i_array = grad[i].astype(np.float32)
                if np.isnan(grad_i_array).any():
                    grad[i] = np.where(
                        np.isnan(grad_i_array), 0.0, grad_i_array
                    ).astype(object)

        # Apply mask
        if mask is not None:
            grad = np.where(mask == 0.0, 0.0, grad)

        # Apply norm bound
        def _apply_norm(grad, object_type=False):
            if (
                grad.dtype != object and np.isinf(grad).any()
            ) or np.isnan(  # pragma: no cover
                grad.astype(np.float32)
            ).any():
                logger.info(
                    "The loss gradient array contains at least one positive or negative infinity."
                )

            if self.norm in [np.inf, "inf"]:
                grad = np.sign(grad)
            elif self.norm == 1:
                if not object_type:
                    ind = tuple(range(1, len(x.shape)))
                else:
                    ind = None
                grad = grad / (np.sum(np.abs(grad), axis=ind, keepdims=True) + tol)
            elif self.norm == 2:
                if not object_type:
                    ind = tuple(range(1, len(x.shape)))
                else:
                    ind = None
                grad = grad / (
                    np.sqrt(np.sum(np.square(grad), axis=ind, keepdims=True)) + tol
                )
            return grad

        if x.dtype == object:
            for i_sample in range(x.shape[0]):
                grad[i_sample] = _apply_norm(grad[i_sample], object_type=True)
                assert x[i_sample].shape == grad[i_sample].shape
        else:
            grad = _apply_norm(grad)

        assert x.shape == grad.shape

        return grad

    def _apply_perturbation(
        self,
        x: np.ndarray,
        perturbation: np.ndarray,
        eps_step: Union[int, float, np.ndarray],
    ) -> np.ndarray:

        perturbation_step = eps_step * perturbation
        if perturbation_step.dtype != object:
            perturbation_step[np.isnan(perturbation_step)] = 0
        else:
            for i, _ in enumerate(perturbation_step):
                perturbation_step_i_array = perturbation_step[i].astype(np.float32)
                if np.isnan(perturbation_step_i_array).any():
                    perturbation_step[i] = np.where(
                        np.isnan(perturbation_step_i_array),
                        0.0,
                        perturbation_step_i_array,
                    ).astype(object)

        x = x + perturbation_step
        if self.estimator.clip_values is not None:
            clip_min, clip_max = self.estimator.clip_values
            if x.dtype == object:
                for i_obj in range(x.shape[0]):
                    x[i_obj] = np.clip(x[i_obj], clip_min, clip_max)
            else:
                x = np.clip(x, clip_min, clip_max)

        return x

    def _compute(
        self,
        x: np.ndarray,
        x_init: np.ndarray,
        y: np.ndarray,
        mask: Optional[np.ndarray],
        eps: Union[int, float, np.ndarray],
        eps_step: Union[int, float, np.ndarray],
        project: bool,
        random_init: bool,
        batch_id_ext: Optional[int] = None,
    ) -> np.ndarray:
        if random_init:
            n = x.shape[0]
            m = np.prod(x.shape[1:]).item()
            random_perturbation = (
                random_sphere(n, m, eps, self.norm)
                .reshape(x.shape)
                .astype(FLOAT_NUMPY)
            )
            if mask is not None:
                random_perturbation = random_perturbation * (
                    mask.astype(FLOAT_NUMPY)
                )
            x_adv = x.astype(FLOAT_NUMPY) + random_perturbation

            if self.estimator.clip_values is not None:
                clip_min, clip_max = self.estimator.clip_values
                x_adv = np.clip(x_adv, clip_min, clip_max)
        else:
            if x.dtype == object:
                x_adv = x.copy()
            else:
                x_adv = x.astype(FLOAT_NUMPY)

        # Compute perturbation with implicit batching
        for batch_id in range(int(np.ceil(x.shape[0] / float(self.batch_size)))):
            if batch_id_ext is None:
                self._batch_id = batch_id
            else:
                self._batch_id = batch_id_ext
            batch_index_1, batch_index_2 = (
                batch_id * self.batch_size,
                (batch_id + 1) * self.batch_size,
            )
            batch_index_2 = min(batch_index_2, x.shape[0])
            batch = x_adv[batch_index_1:batch_index_2]
            batch_labels = y[batch_index_1:batch_index_2]

            mask_batch = mask
            if mask is not None:
                # Here we need to make a distinction: if the masks are different for each input, we need to index
                # those for the current batch. Otherwise (i.e. mask is meant to be broadcasted), keep it as it is.
                if len(mask.shape) == len(x.shape):
                    mask_batch = mask[batch_index_1:batch_index_2]

            # Get perturbation
            perturbation = self._compute_perturbation(batch, batch_labels, mask_batch)

            # Compute batch_eps and batch_eps_step
            if isinstance(eps, np.ndarray) and isinstance(eps_step, np.ndarray):
                if len(eps.shape) == len(x.shape) and eps.shape[0] == x.shape[0]:
                    batch_eps = eps[batch_index_1:batch_index_2]
                    batch_eps_step = eps_step[batch_index_1:batch_index_2]

                else:
                    batch_eps = eps
                    batch_eps_step = eps_step

            else:
                batch_eps = eps
                batch_eps_step = eps_step

            # Apply perturbation and clip
            x_adv[batch_index_1:batch_index_2] = self._apply_perturbation(
                batch, perturbation, batch_eps_step
            )

            if project:
                if x_adv.dtype == object:
                    for i_sample in range(batch_index_1, batch_index_2):
                        if (
                            isinstance(batch_eps, np.ndarray)
                            and batch_eps.shape[0] == x_adv.shape[0]
                        ):
                            perturbation = projection(
                                x_adv[i_sample] - x_init[i_sample],
                                batch_eps[i_sample],
                                self.norm,
                            )

                        else:
                            perturbation = projection(
                                x_adv[i_sample] - x_init[i_sample], batch_eps, self.norm
                            )

                        x_adv[i_sample] = x_init[i_sample] + perturbation

                else:
                    perturbation = projection(
                        x_adv[batch_index_1:batch_index_2]
                        - x_init[batch_index_1:batch_index_2],
                        batch_eps,
                        self.norm,
                    )
                    x_adv[batch_index_1:batch_index_2] = (
                        x_init[batch_index_1:batch_index_2] + perturbation
                    )

        return x_adv

    @staticmethod
    def _get_mask(x: np.ndarray, **kwargs) -> np.ndarray:
        mask = kwargs.get("mask")

        if mask is not None:
            if mask.ndim > x.ndim:  # pragma: no cover
                raise ValueError("Mask shape must be broadcastable to input shape.")

            if not (
                np.issubdtype(mask.dtype, np.floating) or mask.dtype == bool
            ):  # pragma: no cover
                raise ValueError(
                    f"The `mask` has to be either of type np.float32, np.float64 or bool. The provided"
                    f"`mask` is of type {mask.dtype}."
                )

            if (
                np.issubdtype(mask.dtype, np.floating) and np.amin(mask) < 0.0
            ):  # pragma: no cover
                raise ValueError(
                    "The `mask` of type np.float32 or np.float64 requires all elements to be either zero"
                    "or positive values."
                )

        return mask
