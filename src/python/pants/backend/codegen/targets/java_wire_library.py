# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import logging
import os

from pants.backend.jvm.targets.exportable_jvm_library import ExportableJvmLibrary
from pants.base.payload import Payload
from pants.base.payload_field import PrimitiveField


logger = logging.getLogger(__name__)


class JavaWireLibrary(ExportableJvmLibrary):
  """Generates a stub Java library from protobuf IDL files."""

  def __init__(self,
               address=None,
               payload=None,
               service_writer=None,
               service_writer_options=None,
               roots=None,
               registry_class=None,
               enum_options=None,
               no_options=None,
               sources=[],
               **kwargs):
    """
    :param string service_writer: the name of the class to pass as the --service_writer option to
    the Wire compiler.
    :param list service_writer_options: A list of options to pass to the service writer
    :param list roots: passed through to the --roots option of the Wire compiler
    :param string registry_class: fully qualified class name of RegistryClass to create. If in
    doubt, specify com.squareup.wire.SimpleServiceWriter
    :param list enum_options: list of enums to pass to as the --enum-enum_options option, # optional
    :param boolean no_options: boolean that determines if --no_options flag is passed
    """
    def is_virtual(source):
      if isinstance(source, str) or isinstance(source, unicode):
        try:
          abs_path = os.path.join(address.build_file.parent_path, source)
        except:
          return False
        return not os.path.exists(abs_path)
      return False
    virtual_sources = []
    if isinstance(sources, list):
      virtual_sources = [src for src in sources if is_virtual(src)]
      sources = [src for src in sources if src not in virtual_sources]
      logger.debug("java_wire_library(name='{spec},\n  sources=[{real}],\n  "
                   "virtual_sources=[{virtual}]\n)".format(spec=address.spec,
                                                           real=', '.join(sources),
                                                           virtual=', '.join(virtual_sources)))

    payload = payload or Payload()
    payload.add_fields({
      'service_writer': PrimitiveField(service_writer or None),
      'service_writer_options': PrimitiveField(service_writer_options or []),
      'roots': PrimitiveField(roots or []),
      'registry_class': PrimitiveField(registry_class or None),
      'enum_options': PrimitiveField(enum_options or []),
      'no_options': PrimitiveField(no_options or False),
      'virtual_sources': PrimitiveField(virtual_sources),
    })

    if service_writer_options:
      logger.warn('The service_writer_options flag is ignored.')

    super(JavaWireLibrary, self).__init__(address=address, payload=payload, sources=sources,
                                          **kwargs)
    self.add_labels('codegen')
