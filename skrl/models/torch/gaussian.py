from typing import Optional, Union, Sequence

import gym

import torch
from torch.distributions import Normal

from . import Model


class GaussianModel(Model):
    def __init__(self, 
                 observation_space: Union[int, Sequence[int], gym.Space], 
                 action_space: Union[int, Sequence[int], gym.Space], 
                 device: Union[str, torch.device] = "cuda:0", 
                 clip_actions: bool = False, 
                 clip_log_std: bool = True, 
                 min_log_std: float = -20, 
                 max_log_std: float = 2,
                 reduction: str = "sum") -> None:
        """Gaussian model (stochastic model)

        :param observation_space: Observation/state space or shape.
                                  The ``num_observations`` property will contain the size of that space
        :type observation_space: int, sequence of int, gym.Space
        :param action_space: Action space or shape.
                             The ``num_actions`` property will contain the size of that space
        :type action_space: int, sequence of int, gym.Space
        :param device: Device on which a torch tensor is or will be allocated (default: ``"cuda:0"``)
        :type device: str or torch.device, optional
        :param clip_actions: Flag to indicate whether the actions should be clipped to the action space (default: ``False``)
        :type clip_actions: bool, optional
        :param clip_log_std: Flag to indicate whether the log standard deviations should be clipped (default: ``True``)
        :type clip_log_std: bool, optional
        :param min_log_std: Minimum value of the log standard deviation if ``clip_log_std`` is True (default: ``-20``)
        :type min_log_std: float, optional
        :param max_log_std: Maximum value of the log standard deviation if ``clip_log_std`` is True (default: ``2``)
        :type max_log_std: float, optional
        :param reduction: Reduction method for returning the log probability density function: (default: ``"sum"``).
                          Supported values are ``"mean"``, ``"sum"``, ``"prod"`` and ``"none"``. If "``none"``, the log probability density 
                          function is returned as a tensor of shape ``(num_samples, num_actions)`` instead of ``(num_samples, 1)``
        :type reduction: str, optional

        :raises ValueError: If the reduction method is not valid

        Example::

            # define the model
            >>> import torch
            >>> import torch.nn as nn
            >>> from skrl.models.torch import GaussianModel
            >>> 
            >>> class Policy(GaussianModel):
            ...     def __init__(self, observation_space, action_space, device, clip_actions=False,
            ...                  clip_log_std=True, min_log_std=-20, max_log_std=2):
            ...         super().__init__(observation_space, action_space, device, clip_actions,
            ...                          clip_log_std, min_log_std, max_log_std)
            ...
            ...         self.net = nn.Sequential(nn.Linear(self.num_observations, 32),
            ...                                  nn.ELU(),
            ...                                  nn.Linear(32, 32),
            ...                                  nn.ELU(),
            ...                                  nn.Linear(32, self.num_actions))
            ...         self.log_std_parameter = nn.Parameter(torch.zeros(self.num_actions))
            ...
            ...     def compute(self, states, taken_actions, role):
            ...         return self.net(states), self.log_std_parameter
            ...
            >>> # given an observation_space: gym.spaces.Box with shape (60,)
            >>> # and an action_space: gym.spaces.Box with shape (8,)
            >>> model = Policy(observation_space, action_space)
            >>> 
            >>> print(model)
            Policy(
              (net): Sequential(
                (0): Linear(in_features=60, out_features=32, bias=True)
                (1): ELU(alpha=1.0)
                (2): Linear(in_features=32, out_features=32, bias=True)
                (3): ELU(alpha=1.0)
                (4): Linear(in_features=32, out_features=8, bias=True)
              )
            )
        """
        super(GaussianModel, self).__init__(observation_space, action_space, device)
        
        self.clip_actions = clip_actions and issubclass(type(self.action_space), gym.Space)

        if self.clip_actions:
            self.clip_actions_min = torch.tensor(self.action_space.low, device=self.device)
            self.clip_actions_max = torch.tensor(self.action_space.high, device=self.device)
            
            # backward compatibility: torch < 1.9 clamp method does not support tensors
            self._backward_compatibility = tuple(map(int, (torch.__version__.split(".")[:2]))) < (1, 9)

        self.clip_log_std = clip_log_std
        self.log_std_min = min_log_std
        self.log_std_max = max_log_std

        self._log_std = None
        self._num_samples = None
        self._distribution = None
        
        if reduction not in ["mean", "sum", "prod", "none"]:
            raise ValueError("reduction must be one of 'mean', 'sum', 'prod' or 'none'")
        self._reduction = torch.mean if reduction == "mean" else torch.sum if reduction == "sum" \
            else torch.prod if reduction == "prod" else None

    def act(self, 
            states: torch.Tensor, 
            taken_actions: Optional[torch.Tensor] = None, 
            inference: bool = False,
            role: str = "") -> Sequence[torch.Tensor]:
        """Act stochastically in response to the state of the environment

        :param states: Observation/state of the environment used to make the decision
        :type states: torch.Tensor
        :param taken_actions: Actions taken by a policy to the given states (default: ``None``).
                              The use of these actions only makes sense in critical models, e.g.
        :type taken_actions: torch.Tensor, optional
        :param inference: Flag to indicate whether the model is making inference (default: ``False``)
        :type inference: bool, optional
        :param role: Role of the model (default: ``""``)
        :type role: str, optional
        
        :return: Action to be taken by the agent given the state of the environment.
                 The sequence's components are the actions, the log of the probability density function and mean actions
        :rtype: sequence of torch.Tensor

        Example::

            >>> # given a batch of sample states with shape (4096, 60)
            >>> action, log_prob, mean_action = model.act(states)
            >>> print(action.shape, log_prob.shape, mean_action.shape)
            torch.Size([4096, 8]) torch.Size([4096, 1]) torch.Size([4096, 8])
        """
        # map from states/observations to mean actions and log standard deviations
        if self._instantiator_net is None:
            actions_mean, log_std = self.compute(states.to(self.device), 
                                                 taken_actions.to(self.device) if taken_actions is not None else taken_actions,
                                                 role)
        else:
            actions_mean, log_std = self._get_instantiator_output(states.to(self.device), \
                taken_actions.to(self.device) if taken_actions is not None else taken_actions)
        
        # clamp log standard deviations
        if self.clip_log_std:
            log_std = torch.clamp(log_std, self.log_std_min, self.log_std_max)

        self._log_std = log_std
        self._num_samples = actions_mean.shape[0]

        # distribution
        self._distribution = Normal(actions_mean, log_std.exp())

        # sample using the reparameterization trick
        actions = self._distribution.rsample()

        # clip actions
        if self.clip_actions:
            if self._backward_compatibility:
                actions = torch.max(torch.min(actions, self.clip_actions_max), self.clip_actions_min)
            else:
                actions = torch.clamp(actions, min=self.clip_actions_min, max=self.clip_actions_max)
        
        # log of the probability density function
        log_prob = self._distribution.log_prob(actions if taken_actions is None else taken_actions)
        if self._reduction is not None:
            log_prob = self._reduction(log_prob, dim=-1)
        if log_prob.dim() != actions.dim():
            log_prob = log_prob.unsqueeze(-1)

        if inference:
            return actions.detach(), log_prob.detach(), actions_mean.detach()
        return actions, log_prob, actions_mean

    def get_entropy(self) -> torch.Tensor:
        """Compute and return the entropy of the model

        :return: Entropy of the model
        :rtype: torch.Tensor

        Example::

            >>> entropy = model.get_entropy()
            >>> print(entropy.shape)
            torch.Size([4096, 8])
        """
        if self._distribution is None:
            return torch.tensor(0.0, device=self.device)
        return self._distribution.entropy().to(self.device)

    def get_log_std(self) -> torch.Tensor:
        """Return the log standard deviation of the model

        :return: Log standard deviation of the model
        :rtype: torch.Tensor

        Example::

            >>> log_std = model.get_log_std()
            >>> print(log_std.shape)
            torch.Size([4096, 8])
        """
        return self._log_std.repeat(self._num_samples, 1)
    
    def distribution(self) -> torch.distributions.Normal:
        """Get the current distribution of the model

        :return: Distribution of the model
        :rtype: torch.distributions.Normal

        Example::

            >>> distribution = model.distribution()
            >>> print(distribution)
            Normal(loc: torch.Size([4096, 8]), scale: torch.Size([4096, 8]))
        """
        return self._distribution
