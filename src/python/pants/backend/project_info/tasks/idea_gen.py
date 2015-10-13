# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import os
import pkgutil
import shutil
import tempfile
from collections import defaultdict, namedtuple
from xml.dom import minidom

from pants.backend.jvm.targets.jvm_target import JvmTarget
from pants.backend.project_info.tasks.export import ExportTask
from pants.base.build_environment import get_buildroot
from pants.base.generator import Generator, TemplateData
from pants.binaries import binary_util
from pants.scm.git import Git
from pants.util.dirutil import safe_mkdir, safe_walk
from pants.util.memo import memoized_property


_TEMPLATE_BASEDIR = 'templates/idea'


_VERSIONS = {
  '9': '12',  # 9 and 12 are ipr/iml compatible
  '10': '12',  # 10 and 12 are ipr/iml compatible
  '11': '12',  # 11 and 12 are ipr/iml compatible
  '12': '12'
}


_SCALA_VERSION_DEFAULT = '2.9'
_SCALA_VERSIONS = {
  '2.8': 'Scala 2.8',
  _SCALA_VERSION_DEFAULT: 'Scala 2.9',
  '2.10': 'Scala 2.10',
  '2.10-virt': 'Scala 2.10 virtualized'
}


class IdeaGen(ExportTask):

  class Module(namedtuple('Module', ['directory', 'targets'])):
    @property
    def name(self):
      return '{}'.format(os.path.relpath(self.directory, get_buildroot()).replace(os.sep, '-'))
    @property
    def filename(self):
      return '{}.iml'.format(self.name)

  @classmethod
  def register_options(cls, register):
    super(IdeaGen, cls).register_options(register)
    register('--version', choices=sorted(list(_VERSIONS.keys())), default='11',
             help='The IntelliJ IDEA version the project config should be generated for.')
    register('--merge', action='store_true', default=True,
             help='Merge any manual customizations in existing '
                  'Intellij IDEA configuration. If False, manual customizations '
                  'will be over-written.')
    register('--open', action='store_true', default=True,
             help='Attempts to open the generated project in IDEA.')
    register('--bash', action='store_true',
             help='Adds a bash facet to the generated project configuration.')
    register('--scala-language-level',
             choices=_SCALA_VERSIONS.keys(), default=_SCALA_VERSION_DEFAULT,
             help='Set the scala language level used for IDEA linting.')
    register('--scala-maximum-heap-size-mb', type=int, default=512,
             help='Sets the maximum heap size (in megabytes) for scalac.')
    register('--fsc', action='store_true', default=False,
             help='If the project contains any scala targets this specifies the '
                  'fsc compiler should be enabled.')
    register('--java-encoding', default='UTF-8',
             help='Sets the file encoding for java files in this project.')
    register('--java-maximum-heap-size-mb', type=int, default=512,
             help='Sets the maximum heap size (in megabytes) for javac.')
    register('--exclude-maven-target', action='store_true', default=False,
             help="Exclude 'target' directories for directories containing "
                  "pom.xml files.  These directories contain generated code and"
                  "copies of files staged for deployment.")
    register('--maven-style', action='store_true', default=False,
             help="Optimize for a maven-style repo layout.")
    register('--exclude_folders', action='append',
             default=[
               '.pants.d/compile',
               '.pants.d/ivy',
               '.pants.d/python',
               '.pants.d/resources',
               ],
             help='Adds folders to be excluded from the project configuration.')
    register('--annotation-processing-enabled', action='store_true',
             help='Tell IntelliJ IDEA to run annotation processors.')
    register('--annotation-generated-sources-dir', default='generated', advanced=True,
             help='Directory relative to --project-dir to write annotation processor sources.')
    register('--annotation-generated-test-sources-dir', default='generated_tests', advanced=True,
             help='Directory relative to --project-dir to write annotation processor sources.')
    register('--annotation-processor', action='append', advanced=True,
             help='Add a Class name of a specific annotation processor to run.')
    register('--project-name', default='project',
             help='Specifies the name to use for the generated project.')
    register('--project-dir',
             help='Specifies the directory to output the generated project files to.')
    register('--project-cwd',
             help='Specifies the directory the generated project should use as the cwd for '
                  'processes it launches.  Note that specifying this trumps --{0}-project-dir '
                  'and not all project related files will be stored there.'
             .format(cls.options_scope))

  def __init__(self, *args, **kwargs):
    super(IdeaGen, self).__init__(*args, **kwargs)

    self.maven_style = self.get_options().maven_style
    self.intellij_output_dir = os.path.join(self.gen_project_workdir, 'out')
    self.nomerge = not self.get_options().merge
    self.open = self.get_options().open
    self.bash = self.get_options().bash

    self.scala_language_level = _SCALA_VERSIONS.get(
      self.get_options().scala_language_level, None)
    self.scala_maximum_heap_size = self.get_options().scala_maximum_heap_size_mb

    self.fsc = self.get_options().fsc

    self.java_encoding = self.get_options().java_encoding
    self.java_maximum_heap_size = self.get_options().java_maximum_heap_size_mb

    idea_version = _VERSIONS[self.get_options().version]
    self.project_template = os.path.join(_TEMPLATE_BASEDIR,
                                         'project-{}.mustache'.format(idea_version))
    self.module_template = os.path.join(_TEMPLATE_BASEDIR,
                                        'module-{}.mustache'.format(idea_version))

    self.project_filename = os.path.join(self.cwd,
                                         '{}.ipr'.format(self.project_name))
    self.module_filename = os.path.join(self.gen_project_workdir,
                                        '{}.iml'.format(self.project_name))

  @memoized_property
  def gen_project_workdir(self):
    if self.get_options().project_dir:
      return os.path.abspath(os.path.join(self.get_options().project_dir, self.project_name))
    return os.path.abspath(os.path.join(self.workdir, self.__class__.__name__, self.project_name))

  @property
  def project_name(self):
    return self.get_options().project_name

  @memoized_property
  def cwd(self):
    return (
      os.path.abspath(self.get_options().project_cwd) if
      self.get_options().project_cwd else self.gen_project_workdir
    )

  def execute(self):
    targets = self.context.targets()
    blob = self.generate_targets_map(targets)
    configured_modules = dict(self._project_modules(blob))

    outdir = os.path.abspath(self.intellij_output_dir)
    if not os.path.exists(outdir):
      os.makedirs(outdir)

    lang_level = None
    for target in targets:
      if isinstance(target, JvmTarget):
        if lang_level is None or target.platform.target_level > lang_level:
          lang_level = target.platform.target_level

    configured_project = TemplateData(
      root_dir=get_buildroot(),
      outdir=outdir,
      git_root=Git.detect_worktree(),
      modules=configured_modules.values(),
      java=TemplateData(
        encoding=self.java_encoding,
        maximum_heap_size=self.java_maximum_heap_size,
        jdk='{0}.{1}'.format(*lang_level.components[:2]),
        language_level='JDK_{0}_{1}'.format(*lang_level.components[:2]),
      ),
      resource_extensions=[],
      scala=None,
      checkstyle_classpath=';'.join([]),
      debug_port=None,
      annotation_processing=self.annotation_processing_template(blob),
      extra_components=[],
    )

    existing_project_components = None
    if not self.nomerge:
      # Grab the existing components, which may include customized ones.
      existing_project_components = self._parse_xml_component_elements(self.project_filename)

    # Generate (without merging in any extra components).
    safe_mkdir(os.path.abspath(self.intellij_output_dir))

    ipr = self._generate_to_tempfile(Generator(pkgutil.get_data(__name__, self.project_template), project=configured_project))
    imls = [(name, self._generate_to_tempfile(Generator(pkgutil.get_data(__name__, self.module_template), module=module)))
            for name, module in configured_modules.items()]

    if not self.nomerge:
      # Get the names of the components we generated, and then delete the
      # generated files.  Clunky, but performance is not an issue, and this
      # is an easy way to get those component names from the templates.
      extra_project_components = self._get_components_to_merge(existing_project_components, ipr)
      os.remove(ipr)

      # Generate again, with the extra components.
      ipr = self._generate_to_tempfile(Generator(pkgutil.get_data(__name__, self.project_template),
                                                 project=configured_project.extend(extra_components=extra_project_components)))

    self.context.log.info('Generated IntelliJ project in {directory}'
                          .format(directory=self.gen_project_workdir))

    project_directory = os.path.dirname(self.project_filename)
    for existing_project_file in os.listdir(project_directory):
      if existing_project_file.endswith('.iml'):
        os.remove(os.path.join(project_directory, existing_project_file))

    shutil.move(ipr, self.project_filename)
    for index, (name, iml) in enumerate(imls):
      dirname, filename = os.path.split(self.module_filename)
      shutil.move(iml, os.path.join(dirname, name))
    if self.open:
      binary_util.ui_open(self.project_filename)

  def _content_type(self, target_data):
    language = 'java'
    if 'python_interpreter' in target_data:
      language = 'python'
    target_type = target_data['target_type']
    if target_type == 'TEST':
      return None
    # TODO(gm): scala? js? go?
    return '{language}-{type_}'.format(language=language, type_=target_type.lower())

  def _java_language_level(self, blob, target_data):
    if 'platform' not in target_data:
      return None
    target_platform = target_data['platform']
    platforms = blob['jvm_platforms']['platforms']
    target_source_level = platforms[target_platform]['source_level']
    return 'JDK_{0}_{1}'.format(*target_source_level.split('.'))

  def _common_prefix(self, strings):
    prefix = None
    for string in strings:
      if prefix is None:
        prefix = string
        continue
      if string[:len(prefix)] != prefix: # Avoiding startswith to work with lists also.
        for i in range(min(len(prefix), len(string))):
          if prefix[i] != string[i]:
            prefix = prefix[:i]
            break
    return prefix

  def _targets_by_module(self, blob):
    targets_by_module = defaultdict(list)
    for target_spec, target_data in blob['targets'].items():
      if not target_data.get('roots'):
        continue
      target_data['spec'] = target_spec
      root_dir = os.sep.join(self._common_prefix(root['source_root'].split(os.sep)
                                                 for root in target_data['roots']))
      if self.maven_style:
        parts = root_dir.split(os.sep)
        if 'src' in parts:
          root_dir = os.sep.join(parts[:parts.index('src')])
      targets_by_module[root_dir].append(target_data)
    return targets_by_module

  def _choose_target(self, one, two):
    if one is None: return two
    if two is None: return one
    type_precedence = {type_: i for i, type_ in enumerate(('TEST', 'RESOURCE', 'SOURCE'))}
    type_one = type_precedence.get(one['target_type'], -1)
    type_two = type_precedence.get(two['target_type'], -1)
    return two if type_two < type_one else one

  def _dedup_targets(self, all_targets):
    if len(all_targets) < 2:
      return all_targets # Nothing to do.
    targets_by_source_root = defaultdict(list)
    for spec, target in all_targets.items():
      roots = [root['source_root'] for root in target['roots']]
      for root in roots:
        targets_by_source_root[root].append(target)
    for root, targets in targets_by_source_root.items():
      if len(targets) < 2:
        continue
      best = None
      for target in targets: # Pick the best target.
        best = self._choose_target(best, target)
      for target in targets: # Remove the root from the inferior targets.
        if target != best:
          target['roots'] = [r for r in target['roots'] if r['source_root'] != root]
    return {spec: target for spec, target in all_targets.items() if target['roots']}

  def _project_modules(self, blob):
    # blob['targets'] = self._dedup_targets(blob['targets'])

    targets_by_source_root = self._targets_by_module(blob)

    modules = [self.Module(module_dir, targets_by_source_root[module_dir])
               for module_dir in sorted(targets_by_source_root)]
    module_names = { module.name for module in modules }

    module_names_by_target = {}
    for module in modules:
      for target in module.targets:
        module_names_by_target[target['spec']] = module.name

    exclude_folders = []
    if self.get_options().exclude_maven_target:
      exclude_folders += IdeaGen._maven_targets_excludes(get_buildroot())
    exclude_folders += self.get_options().exclude_folders

    # Map of name -> maps of confs to lists of jar paths.
    module_external_libraries = defaultdict(lambda: defaultdict(set))
    # Map of name -> list of names.
    module_dependencies = defaultdict(set)
    # TODO: clean up this deeply nested structure.
    # This builds up the set of libraries each module uses, and the set of other modules each module
    # depends on.
    for module in modules:
      for target in module.targets:
        for target_dependency in target['targets']:
          if blob['targets'][target_dependency].get('pants_target_type') == 'jar_library':
            for library_name in blob['targets'][target_dependency]['libraries']:
              for conf, path in blob['libraries'][library_name].items():
                module_external_libraries[module.name][conf].add(path)
            continue
          if target_dependency not in module_names_by_target:
            continue
          dependency = module_names_by_target[target_dependency]
          if dependency != module.name:
            module_dependencies[module.name].add(dependency)

    # # NB(gmalmquist): HACK! Add every library to every module.
    # # I don't think this actually helped, delete it once you get it working.
    # all_libraries = defaultdict(set)
    # for module, confs in module_external_libraries.items():
    #   for conf in confs:
    #     all_libraries[conf].update(confs[conf])
    # for module in module_external_libraries:
    #   module_external_libraries[module] = all_libraries

    target_type_hierarchy = {
      type_: index for index, type_ in enumerate(('TEST_RESOURCE', 'TEST', 'RESOURCE', 'SOURCE'))
    }

    for module in modules:
      module_dir, targets = module
      sources_by_root = {}
      for target_data in targets:
        for root in target_data['roots']:
          source_root = root['source_root']
          package_prefix = root['package_prefix']
          if not source_root.startswith(module.directory):
            continue
          if self.maven_style:
            # Truncate source root, so that targets are listed under src/test/** rather than
            # src/test/com/foobar/package1/*, src/test/com/foobar/package2/* individually.
            print(source_root)
            package_path_suffix = '{}{}'.format(os.sep, package_prefix.replace('.', os.sep))
            if source_root.endswith(package_path_suffix) and \
                    len(module.directory) < len(source_root) - len(package_path_suffix):
              source_root = source_root[:-len(package_path_suffix)]
              package_prefix = None
            print(source_root)
            print()
            # Infer test target type by the presence of src/test in the path.
            if target_data['target_type'] == 'RESOURCE':
              target_data['target_type'] = 'TEST_RESOURCE'
            elif target_data['target_type'] == 'SOURCE':
              target_data['target_type'] = 'TEST'
          if source_root in sources_by_root:
            # If a target already claimed this source root, pick a single winner based on type.
            previous = target_type_hierarchy.get(sources_by_root[source_root].raw_target_type, -1)
            current = target_type_hierarchy.get(target_data['target_type'], -1)
            if previous < current:
              continue
          sources_by_root[source_root] = (TemplateData(
            path=source_root,
            package_prefix=package_prefix,
            is_test='true' if target_data['target_type'] == 'TEST' else 'false',
            content_type=self._content_type(target_data),
            raw_target_type=target_data['target_type'],
          ))
      sources = sources_by_root.values()

      content_root = TemplateData(
        sources=sources,
        exclude_paths=target_data.get('excludes', ()),
      )

      module_group = None
      if module.name.startswith('.pants.d'): # TODO: get the actual name of the workdir.
        module_group = 'temporary-pants-cache'
      elif '-' in module.name:
        root_module = module.name[:module.name.find('-')]
        if root_module != module.name and root_module not in module_names:
          module_group = root_module

      dependencies = set(module_dependencies[module.name])
      dependencies.add('annotation-processing-code')

      yield module.filename, TemplateData(
        root_dir=module_dir,
        path='$PROJECT_DIR$/{}'.format(module.filename),
        content_roots=[content_root],
        bash=self.bash,
        python='python_interpreter' in target_data,
        scala=False, # ???
        internal_jars=[], # ???
        internal_source_jars=[], # ???
        external_libraries=TemplateData(**{conf: list(jars) for conf, jars in module_external_libraries[module.name].items()}),
        extra_components=[],
        exclude_folders=exclude_folders,
        java_language_level=self._java_language_level(blob, target_data),
        module_dependencies=sorted(dependencies),
        group=module_group,
      )

    yield 'annotation-processing-code.iml', TemplateData(
      root_dir=self.gen_project_workdir,
      path='$PROJECT_DIR$/annotation-processing-code.iml',
      content_roots=[],
      python=False,
      scala=False,
      java_language_level='JDK_1_7',
      group='temporary-pants-cache',
      annotation_processing=self.annotation_processing_template(blob),
      exclude_folders=exclude_folders,
      module_dependencies=[],
    )


  @staticmethod
  def _maven_targets_excludes(repo_root):
    excludes = []
    for (dirpath, dirnames, filenames) in safe_walk(repo_root):
      if "pom.xml" in filenames:
        excludes.append(os.path.join(os.path.relpath(dirpath, start=repo_root), "target"))
    return excludes

  def annotation_processing_template(self, export_blob=None):
    classpath = None
    if export_blob:
      classpath = [lib['default'] for lib in export_blob['libraries'].values() if lib.get('default')]

    return TemplateData(
      enabled=self.get_options().annotation_processing_enabled,
      rel_source_output_dir=os.path.join('..','..','..',
                                         self.get_options().annotation_generated_sources_dir),
      source_output_dir=
      os.path.join(self.gen_project_workdir,
                   self.get_options().annotation_generated_sources_dir),
      rel_test_source_output_dir=os.path.join('..','..','..',
                                              self.get_options().annotation_generated_test_sources_dir),
      test_source_output_dir=
      os.path.join(self.gen_project_workdir,
                   self.get_options().annotation_generated_test_sources_dir),
      processors=[{'class_name' : processor}
                  for processor in self.get_options().annotation_processor],
      classpath=classpath,
    )

  def _generate_to_tempfile(self, generator):
    """Applies the specified generator to a temp file and returns the path to that file.
    We generate into a temp file so that we don't lose any manual customizations on error."""
    (output_fd, output_path) = tempfile.mkstemp()
    with os.fdopen(output_fd, 'w') as output:
      generator.write(output)
    return output_path

  def _parse_xml_component_elements(self, path):
    """Returns a list of pairs (component_name, xml_fragment) where xml_fragment is the xml text of
    that <component> in the specified xml file."""
    if not os.path.exists(path):
      return []  # No existing components.
    dom = minidom.parse(path)
    # .ipr and .iml files both consist of <component> elements directly under a root element.
    return [(x.getAttribute('name'), x.toxml()) for x in dom.getElementsByTagName('component')]

  def _get_components_to_merge(self, mergable_components, path):
    """Returns a list of the <component> fragments in mergable_components that are not
    superceded by a <component> in the specified xml file.
    mergable_components is a list of (name, xml_fragment) pairs."""

    # As a convenience, we use _parse_xml_component_elements to get the
    # superceding component names, ignoring the generated xml fragments.
    # This is fine, since performance is not an issue.
    generated_component_names = set(
      [name for (name, _) in self._parse_xml_component_elements(path)])
    return [x[1] for x in mergable_components if x[0] not in generated_component_names]
