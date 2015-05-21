# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import os

from pants.backend.core.tasks.task import Task
from pants.base.address import SyntheticAddress
from pants.base.build_environment import get_buildroot


class SimpleCodegenTask(Task):
  """Simpler base-class for single-language code-gen.

  Subclasses should implement at minimum: is_gentarget, execute_codegen, and
  sources_generated_by_target.
  """

  @classmethod
  def get_fingerprint_strategy(cls):
    return None

  @property
  def synthetic_target_extra_dependencies(self):
    return []

  def is_gentarget(self, target):
    """Predicate which determines whether the target in question is relevant to this codegen task.
    E.g., the JaxbGen task considers JaxbLibrary targets to be relevant, and nothing else.

    :param target: The target to check.
    :return: True if this class can generate code for the given target, False otherwise.
    """
    raise SimpleCodegenTask.UnimplementedError('is_gentarget')

  def execute_codegen(self, invalid_targets):
    """Generated code for the given list of targets.

    :param invalid_targets: an iterable of targets (a subset of codegen_targets()).
    """
    raise SimpleCodegenTask.UnimplementedError('execute_codegen')

  def sources_generated_by_target(self, target):
    """Predicts what source files will be generated from the given codegen target.

    :param target: the codegen target in question (eg a .proto library).
    :return: an iterable of strings containing the absolute file system paths to source files.
    """
    raise SimpleCodegenTask.UnimplementedError('sources_generated_by_target')

  def codegen_targets(self):
    """Finds codegen targets in the depencency graph.

    :return: an iterable of dependency targets.
    """
    # NOTE(gm): The original CodeGen base just searches through all the targets in the graph and
    # runs the is_gentarget check to see if it wants it. I guess we want to do the same thing here,
    # probably. I don't see any reason not to?
    return self.context.targets(self.is_gentarget)

  def execute(self):
    targets = self.codegen_targets()
    with self.invalidated(targets,
                          invalidate_dependents=True,
                          fingerprint_strategy=self.get_fingerprint_strategy()) as invalidation_check:
      for vts in invalidation_check.invalid_vts_partitioned:
        invalid_targets = vts.targets
        self.execute_codegen(invalid_targets)

      invalid_vts_by_target = dict([(vt.target, vt) for vt in invalidation_check.invalid_vts])
      vts_artifactfiles_pairs = []

      for target in targets:
        synthetic_name = target.id
        sources_rel_path = os.path.relpath(self.workdir, get_buildroot())
        spec_path = '{0}{1}'.format(type(self).__name__, sources_rel_path)
        synthetic_address = SyntheticAddress(spec_path, synthetic_name)
        generated_sources = self.sources_generated_by_target(target)
        relative_generated_sources = [os.path.relpath(src, self.workdir)
                                      for src in generated_sources]

        synthetic_target = self.context.add_new_target(
          address=synthetic_address,
          target_type=self.synthetic_target_type, # From subclasses.
          dependencies=self.synthetic_target_extra_dependencies,
          sources_rel_path=sources_rel_path,
          sources=relative_generated_sources,
          derived_from=target,
        )

        build_graph = self.context.build_graph

        # NOTE(pl): This bypasses the convenience function (Target.inject_dependency) in order
        # to improve performance.  Note that we can walk the transitive dependee subgraph once
        # for transitive invalidation rather than walking a smaller subgraph for every single
        # dependency injected.
        for dependent_address in build_graph.dependents_of(target.address):
          build_graph.inject_dependency(
            dependent=dependent_address,
            dependency=synthetic_target.address,
          )
        # NOTE(pl): See the above comment.  The same note applies.
        for concrete_dependency_address in build_graph.dependencies_of(target.address):
          build_graph.inject_dependency(
            dependent=synthetic_target.address,
            dependency=concrete_dependency_address,
          )
        build_graph.walk_transitive_dependee_graph(
          build_graph.dependencies_of(target.address),
          work=lambda t: t.mark_transitive_invalidation_hash_dirty(),
        )

        if target in self.context.target_roots:
          self.context.target_roots.append(synthetic_target)
        if target in invalid_vts_by_target:
          vts_artifactfiles_pairs.append((invalid_vts_by_target[target], generated_sources))

      if self.artifact_cache_writes_enabled():
        self.update_artifact_cache(vts_artifactfiles_pairs)


  class UnimplementedError(Exception):
    """Raised if subclasses fail to implement vital methods when they are called."""
    def __init__(self, method):
      super(Exception, self).__init__('{} hasn\'t been implemented by subclass!'.format(method))
