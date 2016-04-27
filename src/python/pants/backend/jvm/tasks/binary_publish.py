# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import os

from pants.backend.jvm.targets.jvm_binary import JvmBinary
from pants.backend.jvm.targets.publishable_binary import PublishableBinary
from pants.backend.jvm.tasks.jar_publish import JarPublish
from pants.build_graph.address import Address


class BinaryPublish(JarPublish):
  """Creates a runnable monolithic binary deploy jar."""

  @classmethod
  def prepare(cls, options, round_manager):
    super(BinaryPublish, cls).prepare(options, round_manager)
    round_manager.require('jvm_binaries')

  def execute(self):
    binary_mapping = self.context.products.get('jvm_binaries')
    for target in self.context.targets(predicate=lambda t: isinstance(t, JvmBinary)):
      # TODO: make this conditional on whether the binary should be published.
      publishable_target = self.context.add_new_target(
        address=Address(os.path.join(self.workdir, target.id), publishable_target.name),
        target_type=PublishableBinary,
        dependencies=[target],
        derived_from=target,
      )
      for basedir, jars in binary_mapping.get(target).items():
        self.context.products.get('jars').add(publishable_target, basedir, product_paths=jars)

    super(BinaryPublish, self).execute()
