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

from pants.backend.project_info.tasks.export import ExportTask
from pants.base.build_environment import get_buildroot
from pants.base.generator import Generator, TemplateData
from pants.base.revision import Revision
from pants.binaries import binary_util
from pants.scm.git import Git
from pants.util.dirutil import safe_mkdir, safe_walk
from pants.util.memo import memoized_method, memoized_property


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


_TARGET_TYPE_HIERARCHY = {
  type_: index for index, type_ in enumerate(('TEST_RESOURCE', 'TEST', 'RESOURCE', 'SOURCE'))
}


class IdeaProject(object):
  """Constructs data for an IntelliJ project."""

  AnnotationProcessing = namedtuple('AnnotationProcessing', ['enabled', 'sources_dir',
                                                             'test_sources_dir', 'processors'])

  class Module(object):
    """Represents a module in an IntelliJ project."""

    def __init__(self, directory, targets):
      self.directory = directory
      self.targets = targets
      self.dependencies = set()
      self.libraries = defaultdict(set)
      self.excludes = set()

    @memoized_property
    def name(self):
      return '{}'.format(os.path.relpath(self.directory, get_buildroot()).replace(os.sep, '-'))

    @memoized_property
    def filename(self):
      return '{}.iml'.format(self.name)

  def __init__(self, blob, output_directory, workdir, maven_style=True, exclude_folders=None,
               annotation_processing=None, bash=None, java_encoding=None,
               java_maximum_heap_size=None):
    self.blob = blob
    self.maven_style = maven_style
    self.global_excludes = exclude_folders or ()
    self.workdir = workdir
    self.output_directory = output_directory or os.path.abspath('.')
    self.annotation_processing = annotation_processing
    self.bash = bash
    self.java_encoding = java_encoding
    self.java_maximum_heap_size = java_maximum_heap_size
    self.modules = [self.Module(module_dir, self.targets_by_source_root[module_dir])
                    for module_dir in sorted(self.targets_by_source_root)]
    self._compute_module_dependencies()

  @memoized_method
  def _maven_excludes(self, path):
    excludes = set()
    if path and os.path.exists(path):
      if os.path.isdir(path):
        target = os.path.join(path, 'target')
        if os.path.exists(os.path.join(path, 'pom.xml')) and os.path.exists(target):
          excludes.add(target)
      parent = os.path.dirname(path)
      if parent != path:
        excludes.update(self._maven_excludes(parent))
    return excludes

  def _compute_module_dependencies(self):
    def collect_libraries(module, target_spec):
      if self.blob['targets'][target_spec].get('pants_target_type') != 'jar_library':
        return False
      for library_name in self.blob['targets'][target_spec]['libraries']:
        for conf, path in self.blob['libraries'][library_name].items():
          module.libraries[conf].add(path)
      return True

    for module, target in self.modules_and_targets:
      for target_dependency in target['targets']:
        if collect_libraries(module, target_dependency):
          continue
        if target_dependency not in self.module_names_by_target:
          continue
        dependency = self.module_names_by_target[target_dependency]
        if dependency != module.name:
          module.dependencies.add(dependency)

  @memoized_property
  def module_names_by_target(self):
    module_names_by_target = {}
    for module in self.modules:
      for target in module.targets:
        module_names_by_target[target['spec']] = module.name
    return module_names_by_target

  @memoized_property
  def module_names(self):
    return { module.name for module in self.modules }

  @property
  def modules_and_targets(self):
    for module in self.modules:
      for target in module.targets:
        yield module, target

  @memoized_property
  def targets_by_source_root(self):
    targets_by_module = defaultdict(list)
    for target_spec, target_data in self.blob['targets'].items():
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

  @property
  def annotation_processing_template(self):
    return TemplateData(
      enabled=self.annotation_processing.enabled,
      rel_source_output_dir=os.path.join('..','..','..',
                                         self.annotation_processing.sources_dir),
      source_output_dir=
      os.path.join(self.workdir,
                   self.annotation_processing.sources_dir),
      rel_test_source_output_dir=os.path.join('..','..','..',
                                              self.annotation_processing.test_sources_dir),
      test_source_output_dir=
      os.path.join(self.workdir,
                   self.annotation_processing.test_sources_dir),
      processors=[{'class_name' : processor}
                  for processor in self.annotation_processing.processors],
      classpath=[lib['default'] for lib in self.blob['libraries'].values() if lib.get('default')],
    )

  @memoized_property
  def project_template(self):
    target_levels = {Revision.lenient(platform['target_level'])
                     for platform in self.blob['jvm_platforms']['platforms'].values()}
    lang_level = max(target_levels)

    configured_project = TemplateData(
      root_dir=get_buildroot(),
      outdir=self.output_directory,
      git_root=Git.detect_worktree(),
      modules=self.module_templates_by_filename.values(),
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
      annotation_processing=self.annotation_processing_template,
      extra_components=[],
    )
    return configured_project

  @memoized_property
  def module_templates_by_filename(self):
    return dict(self._generate_module_templates())

  def _generate_module_templates(self):
    for module in self.modules:
      sources_by_root = {}
      for target_data in module.targets:
        for root in target_data['roots']:
          source_root = root['source_root']
          package_prefix = root['package_prefix']
          if not source_root.startswith(module.directory):
            continue
          module.excludes.update(self._maven_excludes(os.path.relpath(source_root, get_buildroot())))
          if self.maven_style:
            # Truncate source root, so that targets are listed under src/test/** rather than
            # src/test/com/foobar/package1/*, src/test/com/foobar/package2/* individually.
            package_path_suffix = '{}{}'.format(os.sep, package_prefix.replace('.', os.sep))
            if source_root.endswith(package_path_suffix) and \
                            len(module.directory) < len(source_root) - len(package_path_suffix):
              source_root = source_root[:-len(package_path_suffix)]
              package_prefix = None
            # Infer test target type by the presence of src/test in the path.
            if target_data['target_type'] == 'RESOURCE':
              target_data['target_type'] = 'TEST_RESOURCE'
            elif target_data['target_type'] == 'SOURCE':
              target_data['target_type'] = 'TEST'
          if source_root in sources_by_root:
            # If a target already claimed this source root, pick a single winner based on type.
            previous = _TARGET_TYPE_HIERARCHY.get(sources_by_root[source_root].raw_target_type, -1)
            current = _TARGET_TYPE_HIERARCHY.get(target_data['target_type'], -1)
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
        if root_module != module.name and root_module not in self.module_names:
          module_group = root_module

      dependencies = module.dependencies
      dependencies.add('annotation-processing-code')

      python = any(target.get('python_interpreter') for target in module.targets)

      yield module.filename, TemplateData(
        root_dir=module.directory,
        path='$PROJECT_DIR$/{}'.format(module.filename),
        content_roots=[content_root],
        bash=self.bash,
        python=python,
        scala=False, # NB(gmalmquist): We don't use Scala, change this if we ever do.
        internal_jars=[], # NB(gmalmquist): These two fields seem to be extraneous.
        internal_source_jars=[],
        external_libraries=TemplateData(**{conf: list(jars) for conf, jars in module.libraries.items()}),
        extra_components=[],
        exclude_folders=sorted(module.excludes | set(self.global_excludes)),
        java_language_level=self._java_language_level(target_data),
        module_dependencies=sorted(dependencies),
        group=module_group,
      )

    yield 'annotation-processing-code.iml', TemplateData(
      root_dir=self.workdir,
      path='$PROJECT_DIR$/annotation-processing-code.iml',
      content_roots=[],
      python=False,
      scala=False,
      java_language_level='JDK_1_7',
      group='temporary-pants-cache',
      annotation_processing=self.annotation_processing_template,
      exclude_folders=self.global_excludes,
      module_dependencies=[],
    )

  def _content_type(self, target_data):
    language = 'java'
    if 'python_interpreter' in target_data:
      language = 'python'
    target_type = target_data['target_type']
    if target_type == 'TEST':
      return None
    # TODO(gm): scala? js? go?
    return '{language}-{type_}'.format(language=language, type_=target_type.lower())

  def _java_language_level(self, target_data):
    if 'platform' not in target_data:
      return None
    target_platform = target_data['platform']
    platforms = self.blob['jvm_platforms']['platforms']
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


class IdeaGen(ExportTask):

  @classmethod
  def register_options(cls, register):
    super(IdeaGen, cls).register_options(register)
    register('--version', choices=sorted(list(_VERSIONS.keys())), default='11',
             help='The IntelliJ IDEA version the project config should be generated for.')
    register('--merge', action='store_true', default=False,
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
    register('--maven-style', action='store_true', default=True,
             help="Optimize for a maven-style repo layout.")
    register('--exclude-folders', action='append',
             default=[
               '.pants.d/compile',
               '.pants.d/ivy',
               '.pants.d/python',
               '.pants.d/resources',
             ],
             help='Adds folders to be excluded from the project configuration.')
    register('--exclude-patterns', action='append', default=[],
             help='Adds patterns for paths to be excluded from the project configuration.')
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

    outdir = os.path.abspath(self.intellij_output_dir)
    if not os.path.exists(outdir):
      os.makedirs(outdir)

    annotation_processing = IdeaProject.AnnotationProcessing(
      enabled=self.get_options().annotation_processing_enabled,
      sources_dir=self.get_options().annotation_generated_sources_dir,
      test_sources_dir=self.get_options().annotation_generated_test_sources_dir,
      processors=self.get_options().annotation_processor,
    )

    project = IdeaProject(blob,
                          output_directory=outdir,
                          workdir=self.gen_project_workdir,
                          maven_style=self.get_options().maven_style,
                          exclude_folders=self.get_options().exclude_folders,
                          annotation_processing=annotation_processing,
                          bash=self.bash,
                          java_encoding=self.java_encoding,
                          java_maximum_heap_size=self.java_maximum_heap_size)

    configured_modules = project.module_templates_by_filename
    configured_project = project.project_template

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
