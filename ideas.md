# Ideas
## Path Feature: finer-grained type presets
The original Path Feature `feature_type` had 5 entries, each with reference real-world width/depth/repeat (mm, all three scaled together by `man_height_mm / REAL_MAN_HEIGHT_MM`) plus a default texture: Footpath (width 600, depth 30, repeat 1000, Brick Gravel), Animal Track (width 400, depth 25, repeat 800, Brick Gravel), Gravel Road (width 2500, depth 40, repeat 1500, Brick Gravel), Country Road (width 4000, depth 20, repeat 2000, Stone Road), Paved Road (width 5000, depth 15, repeat 2000, Stone Road). Replaced by a simpler 3-type set (Simple/Gravel/Paved Road, width as a direct man-height factor, depth/repeat as fixed literal mm — see `path_features._TYPE_DEFAULTS`). If finer-grained types come back, this table plus the old scaled-together formula is the starting point.

## Tap and hole spacing 
Space the hole and tap depending on point height to enforce tile match

## TIle ID  
Put text in the bottom of each tile with the heights and if it has Custom features - Buildings and rivers and more.


## Global Parameters
Adding a normal level (setting all points to something.) This will enable depressions into the terrains such as rivers and lakes.

## Global Parameters (hide)
When typing in the global parameters then when generating the mesh they should be disabled as it makes not sense that you should have access to them after generation. The Re-generate button should be changed to a clean (delete all) and regenrate. 

## Edge
Auto generate an edge for all of the terrain

## Top surface texture
Creating a brush that applies "plowed field", brushes, cobblestones, sheer rock surface


## Removable terrain features
When having trees, bushes, and other features where infantry normally can enter (warmaster rules) a good way of handling this is to remove certain parts of the terrain features. Design a methodology where it is possible to model the tile with the trees, but when printing they are printed next to the tile and have some kind of "lego-like" interface so they can be removed.



# implemented

## easier fit.
making the tabs rounded along the z-axis for the tabs to help this fits.

## Auto STL generation
A button that exports one version of each hex tile along with a json file that enumerates each in how many prints that is has to do. Can this be linked up with Bambu studio API? Using the commandline interface.
