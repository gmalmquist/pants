// Copyright 2016 Pants project contributors (see CONTRIBUTORS.md).
// Licensed under the Apache License, Version 2.0 (see LICENSE).

package org.pantsbuild.testproject.provided_patching;

public class Common {

  public static String getCommonShadowVersion() {
    return new Shadow().getShadowVersion();
  }

}