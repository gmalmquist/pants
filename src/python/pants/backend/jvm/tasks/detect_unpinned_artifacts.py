# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import json
import os
import shutil
import time
from collections import defaultdict
from textwrap import dedent

from pants.backend.jvm.subsystems.jar_dependency_management import JarDependencyManagement
from pants.backend.jvm.targets.jar_library import JarLibrary
from pants.task.console_task import ConsoleTask


class DetectUnpinnedArtifacts(ConsoleTask):

  @classmethod
  def register_options(cls, register):
    super(DetectUnpinnedArtifacts, cls).register_options(register)
    register('--json', action='store_true', default=False,
             help='Output the unpinned jars as a json dict.')

  @classmethod
  def product_types(cls):
    return []

  @classmethod
  def prepare(cls, options, round_manager):
    super(DetectUnpinnedArtifacts, cls).prepare(options, round_manager)
    round_manager.require_data('compile_classpath')

  def __init__(self, *args, **kwargs):
    super(DetectUnpinnedArtifacts, self).__init__(*args, **kwargs)

  def _get_management_spec_and_set(self, library):
    management = JarDependencyManagement.global_instance()
    artifact_set = management.for_target(library)
    if artifact_set is None:
      return None, None
    if library.payload.managed_dependencies:
      management_spec = library.managed_dependencies.address.spec
    else:
      management_spec = management.get_options().default_target
    return management_spec, artifact_set


  def generate_messages_map(self, jar_libraries=None, compile_classpath=None):
    if jar_libraries is None:
      jar_libraries = self.context.targets(lambda t: isinstance(t, JarLibrary))
    if compile_classpath is None:
      compile_classpath = self.context.products.get_data('compile_classpath')

    messages_map = defaultdict(set)
    for library in jar_libraries:
      management_spec, artifact_set = self._get_management_spec_and_set(library)
      for conf, artifact in compile_classpath.get_artifact_classpath_entries_for_targets([library]):
        coordinate = artifact.coordinate
        if artifact_set is None or coordinate not in artifact_set:
          messages_map[management_spec].add(coordinate)

    return {spec: sorted(coords) for spec, coords in messages_map.items()}

  def console_output(self, targets):
    messages_map = self.generate_messages_map()

    if self.get_options().json:
      json_object = {spec: map(str, coords) for spec, coords in messages_map.items()}
      for line in json.dumps(json_object, indent=2, sort_keys=True).split('\n'):
        yield line
    else:
      for management_spec in sorted(messages_map):
        yield 'Coordinates missing from "{}":'.format(management_spec)
        for coordinate in messages_map[management_spec]:
          yield '  {}'.format(coordinate)
