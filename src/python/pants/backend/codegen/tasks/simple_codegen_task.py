# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import os

from twitter.common.collections import OrderedSet

from pants.backend.core.tasks.task import Task
from pants.base.address import SyntheticAddress
from pants.base.build_environment import get_buildroot


class SimpleCodegenTask(Task):
  """A base-class for code generation for a single target language."""

  def __init__(self, *args, **kwargs):
    super(SimpleCodegenTask, self).__init__(*args, **kwargs)
    # This cache saves about 10% real time on my machine running the wire/element example.
    self._generated_sources_cache = {}

  @classmethod
  def register_options(cls, register):
    super(SimpleCodegenTask, cls).register_options(register)
    register('--strategy', choices=['global', 'isolated'], default='isolated',
             help='Selects the compilation strategy to use. The "global" strategy uses a shared '
                  'global directory for all generated code, and the "isolated" strategy uses '
                  'per-target codegen directories.')

  @classmethod
  def get_fingerprint_strategy(cls):
    """Override this method to use a fingerprint strategy other than the default one.

    :return: a fingerprint strategy, or None to use the default strategy.
    """
    return None

  def synthetic_target_extra_dependencies(self, target):
    """Gets any extra dependencies generated synthetic targets should have.

    This method is optional for subclasses to implement, because some code generators may have no
    extra dependencies.
    :param Target target: the Target from which we are generating a synthetic Target. E.g., 'target'
    might be a JavaProtobufLibrary, whose corresponding synthetic Target would be a JavaLibrary.
    It may not be necessary to use this parameter depending on the details of the subclass.
    :return: a list of dependencies.
    """
    return []

  @property
  def forced_codegen_strategy(self):
    """Allows subclasses to force a particular code generation strategy ('isolated' or 'global').

    This overrides the --strategy the user specifies. This is useful if only one strategy is
    implemented/supported.
    :return: the forced code generation strategy, or None if both options are supported.
    """
    return None

  @property
  def synthetic_target_type(self):
    """The type of target this codegen task generates.

    For example, the target type for JaxbGen would simply be JavaLibrary.
    :return: a type (class) that inherits from Target.
    """
    raise NotImplementedError

  def is_gentarget(self, target):
    """Predicate which determines whether the target in question is relevant to this codegen task.

    E.g., the JaxbGen task considers JaxbLibrary targets to be relevant, and nothing else.
    :param Target target: The target to check.
    :return: True if this class can generate code for the given target, False otherwise.
    """
    raise NotImplementedError

  def execute_codegen(self, invalid_targets):
    """Generated code for the given list of targets.

    :param invalid_targets: an iterable of targets (a subset of codegen_targets()).
    """
    raise NotImplementedError

  def sources_generated_by_target(self, target):
    """Predicts what source files will be generated from the given codegen target.

    This method may be unimplemented if forced_codegen_strategy returns 'isolated'.
    :param Target target: the codegen target in question (eg a .proto library).
    :return: an iterable of strings containing the file system paths to the sources files.
    """
    raise NotImplementedError

  def codegen_targets(self):
    """Finds codegen targets in the dependency graph.

    :return: an iterable of dependency targets.
    """
    return self.context.targets(self.is_gentarget)

  @property
  def codegen_strategy(self):
    strategy = self.forced_codegen_strategy
    if strategy is None:
      strategy = self.get_options().strategy
    return strategy

  @classmethod
  def _codegen_workdir_suffix(cls, target, strategy):
    suffixes = {
      'isolated': os.path.join('isolated', target.id),
      'global': 'global',
    }
    return suffixes[strategy]

  def codegen_workdir(self, target):
    """The path to the directory code should be generated in.

    E.g., this might be something like /home/user/repo/.pants.d/gen/jaxb/...
    Generally, subclasses should not need to override this method. If they do, it is crucial that
    the implementation is /deterministic/ -- that is, the return value of this method should always
    be the same for the same input target.
    :param Target target: the codegen target (e.g., a java_protobuf_library).
    :return: The absolute file path.
    """
    return os.path.join(self.workdir, self._codegen_workdir_suffix(target, self.codegen_strategy))

  def _execute_strategy_isolated(self, targets):
    for target in targets:
      self.execute_codegen([target])

  def _execute_strategy_global(self, targets):
    self.execute_codegen(targets)

  def _find_sources_generated_by_target(self, target):
    if target.id in self._generated_sources_cache:
      for source in self._generated_sources_cache[target.id]:
        yield source
    target_workdir = self.codegen_workdir(target)
    if not os.path.exists(target_workdir):
      return
    # breadth-first search to find generated files.
    frontier = [target_workdir]
    while frontier:
      path = frontier.pop(0)
      if os.path.isdir(path):
        frontier.extend([os.path.join(path, file_name) for file_name in os.listdir(path)])
      else:
        yield path

  def _find_sources_generated_by_dependencies(self, target, relative=False):
    sources = OrderedSet()
    def add_sources(dep):
      if dep is not target:
        dep_sources = self._find_sources_generated_by_target(dep)
        if relative:
          dep_sources = [self._relative_source(dep, source) for source in dep_sources]
        sources.update(dep_sources)
    target.walk(add_sources)
    return sources

  def _relative_source(self, target, source):
    return os.path.relpath(source, self.codegen_workdir(target))

  def _find_sources_strictly_generated_by_target(self, target):
    if target.id in self._generated_sources_cache:
      return self._generated_sources_cache[target.id]
    by_target = OrderedSet(self._find_sources_generated_by_target(target))
    by_dependencies = self._find_sources_generated_by_dependencies(target, relative=True)
    strict = [t for t in by_target if self._relative_source(target, t) not in by_dependencies]
    # TODO(gm): remove debug code or actually log it in the logger.
    print('\n[Target] {name}\n\t  {by_target}\n[Parents]\n\t  {by_parents}\n[Strict]\n\t  {strict}'
      .format(name=target.address.spec,
              by_target='\n\t  '.join(os.path.basename(s) for s in by_target),
              by_parents='\n\t  '.join(os.path.basename(s) for s in by_dependencies),
              strict='\n\t  '.join(os.path.basename(s) for s in strict)))
    self._generated_sources_cache[target.id] = strict
    return strict

  def execute(self):
    execute_strategies = {
      'isolated': self._execute_strategy_isolated,
      'global': self._execute_strategy_global,
    }
    sources_strategies = {
      'isolated': self._find_sources_strictly_generated_by_target,
      'global': self.sources_generated_by_target,
    }

    targets = self.codegen_targets()
    with self.invalidated(targets,
                          invalidate_dependents=True,
                          fingerprint_strategy=self.get_fingerprint_strategy()) as invalidation_check:
      # NOTE(gm): the protobuf integration test that tests ordering fails if invalid_vts is used
      # rather than invalid_vts_partitioned, because it makes 'global' compile all the targets
      # separately (in 3 groups instead of 1) for some reason.
      for vts in invalidation_check.invalid_vts_partitioned:
        execute_strategies[self.codegen_strategy](vts.targets)

      invalid_vts_by_target = dict([(vt.target, vt) for vt in invalidation_check.invalid_vts])
      vts_artifactfiles_pairs = []

      for target in targets:
        target_workdir = self.codegen_workdir(target)
        synthetic_name = target.id
        sources_rel_path = os.path.relpath(target_workdir, get_buildroot())
        spec_path = '{0}{1}'.format(type(self).__name__, sources_rel_path)
        synthetic_address = SyntheticAddress(spec_path, synthetic_name)
        raw_generated_sources = list(sources_strategies[self.codegen_strategy](target))
        # Make the sources robust regardless of whether subclasses return relative paths, or
        # absolute paths that are subclasses of the workdir.
        generated_sources = [src if src.startswith(target_workdir)
                             else os.path.join(target_workdir, src)
                             for src in raw_generated_sources]
        relative_generated_sources = [os.path.relpath(src, target_workdir)
                                      for src in generated_sources]

        self.target = self.context.add_new_target(
          address=synthetic_address,
          target_type=self.synthetic_target_type,
          dependencies=self.synthetic_target_extra_dependencies(target),
          sources_rel_path=sources_rel_path,
          sources=relative_generated_sources,
          derived_from=target,
          provides=target.provides,
        )
        synthetic_target = self.target

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
