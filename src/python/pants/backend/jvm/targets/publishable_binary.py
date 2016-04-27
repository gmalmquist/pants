# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

from pants.build_graph.target import Target


class PublishableBinary(Target):
  """A publishable binary. These should only be created synthetically."""

  def __init__(self, *args, **kwargs):
    """A publishable binary."""
    super(PublishableBinary, self).__init__(*args, **kwargs)
    self.add_labels('exportable')

  @property
  def provides(self):
    return True
