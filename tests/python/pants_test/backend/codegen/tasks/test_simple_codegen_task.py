# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import os
from textwrap import dedent

from twitter.common.collections import OrderedSet

from pants.backend.codegen.register import build_file_aliases as register_codegen
from pants.backend.codegen.targets.java_wire_library import JavaWireLibrary
from pants.backend.codegen.tasks.simple_codegen_task import SimpleCodegenTask
from pants.backend.core.register import build_file_aliases as register_core
from pants.backend.jvm.targets.java_library import JavaLibrary
from pants.base.source_root import SourceRoot
from pants.base.validation import assert_list
from pants.util.contextutil import temporary_file
from pants_test.tasks.task_test_base import TaskTestBase


class SimpleCodegenTaskTest(TaskTestBase):
  @classmethod
  def task_type(cls):
    return SimpleCodegenTask

  @property
  def alias_groups(self):
      return register_core().merge(register_codegen())

  def _create_subclass_task(self, subclass_type, target_roots=None, **options):
    task_type, scope = self.synthesize_task_subtype(subclass_type)
    self.set_options_for_scope(scope, **options)
    context = self.context(for_task_types=[task_type], target_roots=target_roots)
    return task_type(context, self._test_workdir)

  def test_codegen_strategy(self):
    task = self.create_task(self.context())

    self.set_options_for_scope(task.options_scope, strategy='global')
    task = self.create_task(self.context())
    self.assertEqual('global', task.get_options().strategy)
    self.assertEqual('global', task.codegen_strategy)

    self.set_options_for_scope(task.options_scope, strategy='isolated')
    task = self.create_task(self.context())
    self.assertEqual('isolated', task.codegen_strategy)

    class TestGen(SimpleCodegenTask):
      @property
      def forced_codegen_strategy(self):
        return 'global'

    task = self._create_subclass_task(TestGen, strategy='global')
    self.assertEqual('global', task.codegen_strategy)
    task = self._create_subclass_task(TestGen, strategy='isolated')
    self.assertEqual('global', task.codegen_strategy)

  def test_codegen_workdir_suffix(self):
    target_specs = [
      'project/src/main/foogen/foo-lib:foo-target-a',
      'project/src/main/foogen/foo-lib:foo-target-b',
      'project/src/main/foogen/foo-bar:foo-target-a',
      'project/src/main/genfoo/foo-bar:foo-target-a',
    ]
    spec_paths = set([spec.split(':')[0] for spec in target_specs])
    spec_names = set([spec.split(':')[1] for spec in target_specs])
    for spec_path in spec_paths:
      parent_dir = os.path.dirname(spec_path)
      if not os.path.exists(parent_dir):
        self.create_dir(parent_dir)
      self.create_file('{0}/BUILD'.format(spec_path), '\n'.join([dedent('''
          java_protobuf_library(name='{name}',
            sources=[],
            dependencies=[],
          )
        '''.format(name=spec_name)) for spec_name in spec_names]))

    targets = set([self.target(spec) for spec in target_specs])

    get_suffix = SimpleCodegenTask._codegen_workdir_suffix

    for target in targets:
      self.assertEqual('global', get_suffix(target, 'global'))
      self.assertTrue('isolated' in get_suffix(target, 'isolated'))

    global_dirs = set(get_suffix(target, 'global') for target in targets)
    isolated_dirs = set(get_suffix(target, 'isolated') for target in targets)

    self.assertEqual(1, len(global_dirs), 'There should only be one global directory suffix!')
    self.assertEqual(len(targets), len(isolated_dirs),
                     'There should be exactly one directory suffix per unique target!')

    for target in targets:
      for strategy in ('global', 'isolated',):
        suffix = None
        for i in range(5):
          curr_suffix = get_suffix(target, strategy)
          if i > 0:
            self.assertEqual(suffix, curr_suffix,
                             'Suffix should not be unstable for the same target and strategy! '
                             '({target} with {strategy} got {suffix_a} and {suffix_b})'
                             .format(target=target.id,
                                     strategy=strategy,
                                     suffix_a=suffix,
                                     suffix_b=curr_suffix))
          suffix = curr_suffix

  def test_execute(self):
    proto_suffixes = ['a', 'b', 'c',]

    self.add_to_build_file('gen-lib', '\n'.join(dedent('''
      java_wire_library(name='{suffix}',
        sources=['org/pantsbuild/example/foo{suffix}.proto'],
      )
    ''').format(suffix=suffix) for suffix in proto_suffixes))

    for suffix in proto_suffixes:
      self.create_file('gen-lib/org/pantsbuild/example/foo{suffix}.proto'.format(suffix=suffix),
                       dedent('''
                                package org.pantsbuild.example;

                                message Foo%s {
                                  optional string bar = 1;
                                }
                              ''' % suffix)) # .format() chokes because of the message braces.

    targets = [self.target('gen-lib:{suffix}'.format(suffix=suffix)) for suffix in proto_suffixes]

    test_base = self
    class TestGen(SimpleCodegenTask):
      def is_gentarget(self, target):
        return isinstance(target, JavaWireLibrary)
      def execute_codegen(self, invalid_targets):
        if self.codegen_strategy == 'isolated':
          test_base.assertEqual(1, len(invalid_targets),
                                'Codegen should execute individually in isolated mode.')
        elif self.codegen_strategy == 'global':
          test_base.assertEqual(len(targets), len(invalid_targets),
                                'Codegen should execute all together in global mode.')
        for target in invalid_targets:
          for path in self.sources_generated_by_target(target):
            message_name = os.path.basename(path).split('.')[0]
            if not os.path.exists(os.path.join(test_base.build_root, os.path.basename(path))):
              test_base.create_dir(os.path.basename(path))
            test_base.create_file(path)
            f = open(path, 'w')
            f.write(dedent('''
              package org.pantsbuild.example;

              public class %s {
                // nothing.
              }
            ''' % message_name))
            f.close()
      def sources_generated_by_target(self, target):
        test_base.assertEqual('global', self.codegen_strategy,
                              'sources_generated_by_target should only be called for '
                              'strategy=global.')
        for source in target.sources_relative_to_buildroot():
          source = os.path.join(test_base.build_root, source)
          f = open(source, 'r')
          lines = f.readlines()
          f.close()
          for line in lines:
            if 'message ' in line:
              message_name = line.strip().split(' ')[1]
              yield os.path.join(self.codegen_workdir(target),
                                 '{name}.java'.format(name=message_name))
      def _find_sources_generated_by_target(self, target):
        test_base.assertEqual('isolated', self.codegen_strategy,
                              '_find_sources_generated_by_target should only be called for '
                              'strategy=isolated.')
        return super(TestGen, self)._find_sources_generated_by_target(target)
      @property
      def synthetic_target_type(self):
        return JavaLibrary

    for strategy in ('global', 'isolated',):
      task = self._create_subclass_task(TestGen, target_roots=targets, strategy=strategy)

      expected_targets = set(targets)
      found_targets = set(task.codegen_targets())
      self.assertEqual(expected_targets, found_targets,
                       'TestGen failed to find codegen target {expected}! Found: [{found}].'
                       .format(expected=', '.join(t.id for t in expected_targets),
                               found=', '.join(t.id for t in found_targets)))
      task.execute()
