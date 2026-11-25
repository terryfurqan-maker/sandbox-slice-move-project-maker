# Midland Valley Move 2016 `.move` / `.movd` Format Reference

A Move 2016 project consists of:
1. `<Project>.move`: Small XML pointer (`<!DOCTYPE MVE>`) containing `<project>path/to/<Project>.movd</project>`.
2. `<Project>.movd/`: Project directory containing:
   * `wkt.txt`: Coordinate system WKT string.
   * `StratColumn-9.xml`: Default stratigraphic column XML.
   * `index.move`: Mirror of `<Project>.move`.
   * `geos/HorizontalImage/<id%100:02d>/<id:015d>.png`: PNG copy of horizontal map images.
   * `geos/SegyImage/<id%100:02d>/<id:015d>.png`: PNG copy of vertical section images.
   * `db/`: LevelDB database storing serialized objects:
     * `geo|<id:015d>`: Protobuf metadata and bounding box (`little-endian` `double` for fields `4..9`) for `HorizontalImage`, `PolyLine`, `Post2`, and `SegyImage`.
     * `chunkgeo|<id:015d>|vertex00000`: Vertex array in `QDataStream` format (`big-endian` `uint64` index + **big-endian** `double` `x, y, z` + `uint16` `0` per vertex, 34 bytes/vertex).
     * `setgeo|<id:015d>`: Protobuf `Section` set linking member IDs (`[polyline_id, post1_id, post2_id, segyimage_id]`).
     * `view|00015` (`3DView`), `view|00016` (`2DMapView`), `view|00018` (`2DSectionView`), and `itemview|<view>|<obj>`: View camera and visibility state.
     * `model` & `model_bbox`: Project timestamp metadata and overall `ModelBoundingBox2` (`QDataStream`, big-endian doubles).
