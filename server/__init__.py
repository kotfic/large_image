#!/usr/bin/env python
# -*- coding: utf-8 -*-

###############################################################################
#  Copyright Kitware Inc.
#
#  Licensed under the Apache License, Version 2.0 ( the "License" );
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################

try:
    from .base import *  # noqa
except ImportError as exc:
    # If our import failed because either girder or a girder plugin is
    # unavailable, log it and start anyway (we may be running in a girder-less
    # environment).  Otherwise, reraise the exception -- something else went
    # wrong.
    if 'plugins' not in repr(exc.args) and 'girder' not in repr(exc.args):
        raise
    import logging as logger
    logger.getLogger().setLevel(logger.INFO)
    logger.debug('Girder is unavailable.  Run as a girder plugin for girder '
                 'access.')
    girder = None
