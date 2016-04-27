# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

from pants.base.payload import Payload
from pants.build_graph.target import Target


class PublishableBinary(Target):
  """A publishable binary. These should only be created synthetically."""

  def __init__(self, payload=None, provides_target=None, *args, **kwargs):
    """A publishable binary."""
    payload = payload or Payload()
    super(PublishableBinary, self).__init__(payload=payload, *args, **kwargs)
    self.add_labels('exportable')
    self._provides_target = provides_target

  @property
  def provides(self):
    return self._provides_target.provides

  def __getattribute__(self, name):
    if name in ('get_artifact_info', 'jar_dependencies'):
      return getattr(self._provides_target, name)
    return super(PublishableBinary, self).__getattribute__(name)
