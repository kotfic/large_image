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

import base64
import itertools
import math
import six
from six import BytesIO
from six.moves import range

from .base import FileTileSource, TileSourceException
from ..cache_util import LruCacheMetaclass, methodcache
from .tiff_reader import TiledTiffDirectory, TiffException, \
    InvalidOperationTiffException, IOTiffException, ValidationTiffException

try:
    import girder
    from girder import logger
    from .base import GirderTileSource
except ImportError:
    girder = None
    import logging as logger
    logger.getLogger().setLevel(logger.INFO)
from .base import TILE_FORMAT_PIL

try:
    import PIL.Image
except ImportError:
    PIL = None


@six.add_metaclass(LruCacheMetaclass)
class TiffFileTileSource(FileTileSource):
    """
    Provides tile access to TIFF files.
    """
    cacheName = 'tilesource'
    name = 'tifffile'

    def __init__(self, item, **kwargs):
        super(TiffFileTileSource, self).__init__(item, **kwargs)

        largeImagePath = self._getLargeImagePath()
        lastException = None

        # Query all know directories in the tif file.  Only keep track of
        # directories that contain tiled images.
        alldir = []
        for directoryNum in itertools.count():
            try:
                td = TiledTiffDirectory(largeImagePath, directoryNum)
            except ValidationTiffException as exc:
                lastException = exc
                continue
            except TiffException as exc:
                if not lastException:
                    lastException = exc
                break
            if not td.tileWidth or not td.tileHeight:
                continue
            # Calculate the tile level, where 0 is a single tile, 1 is up to a
            # set of 2x2 tiles, 2 is 4x4, etc.
            level = max(0, int(math.ceil(math.log(max(
                float(td.imageWidth) / td.tileWidth,
                float(td.imageHeight) / td.tileHeight)) / math.log(2))))
            # Store information for sorting with the directory.
            alldir.append((td.tileWidth * td.tileHeight, level, td))
        # If there are no tiled images, raise an exception.
        if not len(alldir):
            msg = 'File %s didn\'t meet requirements for tile source: %s' % (
                largeImagePath, lastException)
            logger.debug(msg)
            raise TileSourceException(msg)
        # Sort the known directories by image area (width * height).  Given
        # equal area, sort by the level.
        alldir.sort()
        # The highest resolution image is our preferred image
        highest = alldir[-1][-1]
        directories = {}
        # Discard any images that use a different tiling scheme than our
        # preferred image
        for dir in alldir:
            td = dir[-1]
            level = dir[-2]
            if (td.tileWidth != highest.tileWidth or
                    td.tileHeight != highest.tileHeight):
                continue
            directories[level] = td

        # Sort the directories so that the highest resolution is the last one;
        # if a level is missing, put a None value in its place.
        self._tiffDirectories = [directories.get(key) for key in
                                 range(max(directories.keys()) + 1)]

        self.tileWidth = highest.tileWidth
        self.tileHeight = highest.tileHeight
        self.levels = len(self._tiffDirectories)
        self.sizeX = highest.imageWidth
        self.sizeY = highest.imageHeight

    def getNativeMagnification(self):
        """
        Get the magnification at a particular level.

        :return: magnification, width of a pixel in mm, height of a pixel in mm.
        """
        pixelInfo = self._tiffDirectories[-1].pixelInfo
        mm_x = pixelInfo.get('mm_x')
        mm_y = pixelInfo.get('mm_y')
        # Estimate the magnification if we don't have a direct value
        mag = pixelInfo.get('magnification', 0.01 / mm_x if mm_x else None)
        return {
            'magnification': mag,
            'mm_x': mm_x,
            'mm_y': mm_y,
        }

    @methodcache()
    def getTile(self, x, y, z, pilImageAllowed=False, sparseFallback=False,
                **kwargs):
        try:
            if self._tiffDirectories[z] is None:
                if sparseFallback:
                    raise IOTiffException('Missing z level %d' % z)
                tile = self.getTileFromEmptyDirectory(x, y, z)
                format = TILE_FORMAT_PIL
            else:
                tile = self._tiffDirectories[z].getTile(x, y)
                format = 'JPEG'
            if PIL and isinstance(tile, PIL.Image.Image):
                format = TILE_FORMAT_PIL
            return self._outputTile(tile, format, x, y, z, pilImageAllowed,
                                    **kwargs)
        except IndexError:
            raise TileSourceException('z layer does not exist')
        except InvalidOperationTiffException as e:
            raise TileSourceException(e.message)
        except IOTiffException as e:
            if sparseFallback and z and PIL:
                image = self.getTile(x / 2, y / 2, z - 1, pilImageAllowed=True,
                                     sparseFallback=sparseFallback, edge=False)
                if not isinstance(image, PIL.Image.Image):
                    image = PIL.Image.open(BytesIO(image))
                image = image.crop((
                    self.tileWidth / 2 if x % 2 else 0,
                    self.tileHeight / 2 if y % 2 else 0,
                    self.tileWidth if x % 2 else self.tileWidth / 2,
                    self.tileHeight if y % 2 else self.tileHeight / 2))
                image = image.resize((self.tileWidth, self.tileHeight))
                return self._outputTile(image, 'PIL', x, y, z, pilImageAllowed,
                                        **kwargs)
            raise TileSourceException('Internal I/O failure: %s' % e.message)

    def getTileFromEmptyDirectory(self, x, y, z):
        """
        Given the x, y, z tile location in an unpopulated level, get tiles from
        higher resolution levels to make the lower-res tile.

        :param x: location of tile within original level.
        :param y: location of tile within original level.
        :param z: original level.
        :returns: tile in PIL format.
        """
        scale = 1
        while self._tiffDirectories[z] is None:
            scale *= 2
            z += 1
        tile = PIL.Image.new(
            'RGBA', (self.tileWidth * scale, self.tileHeight * scale))
        maxX = 2.0 ** (z + 1 - self.levels) * self.sizeX / self.tileWidth
        maxY = 2.0 ** (z + 1 - self.levels) * self.sizeY / self.tileHeight
        for newX in range(scale):
            for newY in range(scale):
                if ((newX or newY) and ((x * scale + newX) >= maxX or
                                        (y * scale + newY) >= maxY)):
                    continue
                subtile = self.getTile(
                    x * scale + newX, y * scale + newY, z,
                    pilImageAllowed=True, sparseFallback=True, edge=False)
                if not isinstance(subtile, PIL.Image.Image):
                    subtile = PIL.Image.open(BytesIO(subtile))
                tile.paste(subtile, (newX * self.tileWidth,
                                     newY * self.tileHeight))
        return tile.resize((self.tileWidth, self.tileHeight),
                           PIL.Image.LANCZOS)

    def getPreferredLevel(self, level):
        """
        Given a desired level (0 is minimum resolution, self.levels - 1 is max
        resolution), return the level that contains actual data that is no
        lower resolution.

        :param level: desired level
        :returns level: a level with actual data that is no lower resolution.
        """
        level = max(0, min(level, self.levels - 1))
        while self._tiffDirectories[level] is None and level < self.levels - 1:
            level += 1
        return level

    def getAssociatedImagesList(self):
        """
        Get a list of all associated images.

        :return: the list of image keys.
        """
        imageList = set()
        for td in self._tiffDirectories:
            imageList |= set(td._embeddedImages)
        return sorted(imageList)

    def _getAssociatedImage(self, imageKey):
        """
        Get an associated image in PIL format.

        :param imageKey: the key of the associated image.
        :return: the image in PIL format or None.
        """
        for td in self._tiffDirectories:
            if imageKey in td._embeddedImages:
                image = PIL.Image.open(BytesIO(base64.b64decode(td._embeddedImages[imageKey])))
                return image
        return None


if girder:
    class TiffGirderTileSource(TiffFileTileSource, GirderTileSource):
        """
        Provides tile access to Girder items with a TIFF file.
        """
        cacheName = 'tilesource'
        name = 'tiff'
