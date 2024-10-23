import tomllib
import sys
import json
from copy import deepcopy
from pprint import pprint

valid_shell_types = ['wiggle', 'courtyard']
valid_jig_types = ['TH_soldering', 'component_fitting']
valid_base_types = ["mesh", "solid"]
valid_insertions = ["top", "bottom"]

TH_component_shell_value_keys = ["thickness", "gap", "clearance_from_pcb"]
SMD_default_value_keys = ['clearance_from_shells', 'gap_from_shells']
def transfer_default_values(default_cfg, cfg):
    """ transfer default values from default_cfg to cfg """
    for key, value in default_cfg.items():
        if type(value) is not dict:
            if key not in cfg:
                cfg[key] = deepcopy(value)
        else:
            if key not in cfg:
                cfg[key] = deepcopy(value)
            else:
                # recurse
                transfer_default_values(default_cfg[key], cfg[key])

def load(configFile, TH_ref_names, SMD_ref_names):
    """ load configuration file, validate against TH reference names"""
    default_config_text = get_default()
    default_cfg = tomllib.loads(default_config_text)

    if configFile:
        config_text = open(configFile, 'r').read()
        cfg = tomllib.load(open(configFile,'rb'))
        #print(json.dumps(cfg, indent=2))
    else:
        config_text = default_config_text
        cfg = deepcopy(default_cfg)

    transfer_default_values(default_cfg, cfg)

    base_type = cfg['holder']['base']['type']
    if base_type not in valid_base_types:
        raise ValueError(f"Bad value holder.base.type={base_type}. Recognized values are:{valid_base_types}")

    jig_type = cfg['jig']['type']
    if jig_type not in valid_jig_types:
        raise ValueError(f"Bad value jig.type={jig_type}. Recognized values are:{valid_jig_types}")

    shell_type = cfg['TH']['component_shell']['type']
    if shell_type not in valid_shell_types:
        raise ValueError(f"Bad value TH.component_shell.type={shell_type}. Recognized values are:{valid_shell_types}")

    insertion = cfg['TH']['component_shell']['component_insertion']
    if insertion not in valid_insertions:
        raise ValueError(f"Bad value TH.component_shell.component_insertion={insertion}. Recognized values are:{valid_insertions}")

    for key in cfg['TH']['component_shell']:
        if key in default_cfg['TH']['component_shell'].keys():
            continue
        if key in TH_ref_names:
            continue
        raise ValueError(f"Can't use TH.component_shell.{key}. No such TH component on the board.")

    # Expand component level defaults
    for ref in TH_ref_names:
        if ref in default_cfg['TH']['component_shell'].keys():
            continue
        if ref not in cfg['TH']:
            cfg['TH'][ref] = { 'component_shell' : deepcopy(default_cfg['TH']['component_shell']) }
            continue

        try:
            ref_cs_type = cfg['TH'][ref]['component_shell']['type']
            if ref_cs_type not in valid_shell_types:
                raise ValueError(
                    f"Bad value TH.{ref}.component_shell.type={ref_cs_type}. Recognized values are:{valid_shell_types}")
        except KeyError:
            cfg['TH'][ref]['component_shell']['type'] = shell_type

        try:
            ref_cs_insertion = cfg['TH'][ref]['component_shell']['component_insertion']
            if ref_cs_insertion not in valid_insertions:
                raise ValueError(
                    f"Bad value TH.{ref}.component_shell.component_insertion={ref_cs_insertion}. Recognized values are:{valid_insertions}")
        except KeyError:
            cfg['TH'][ref]['component_shell']['component_insertion'] = insertion
        
        for other_key in TH_component_shell_value_keys:
            if other_key not in cfg['TH'][ref]['component_shell']:
                cfg['TH'][ref]['component_shell'][other_key] = default_cfg['TH']['component_shell'][other_key]

        if cfg['TH'][ref]['component_shell']['component_insertion'] == 'bottom':
            # Both wiggle and courtyard are compatible with bottom insertion. If not
            # these, we default to wiggle for its usability characteristics
            if cfg['TH'][ref]['component_shell']['type'] not in ["wiggle", "courtyard"]:
                cfg['TH'][ref]['component_shell']['type'] = "wiggle"

    for key in cfg['SMD']:
        if key in default_cfg['SMD'].keys():
            continue
        if key in SMD_ref_names:
            continue
        raise ValueError(f"Can't use SMD.{key}. No such SMD component on the board.")

    # Expand component level defaults
    for ref in SMD_ref_names:
        if ref in default_cfg['SMD'].keys():
            continue
        if ref not in cfg['SMD']:
            cfg['SMD'][ref] = deepcopy(default_cfg['SMD'])
            continue
        # set default values, if not already present
        for other_key in SMD_default_value_keys:
            if other_key not in cfg['SMD'][ref]:
                cfg['SMD'][ref][other_key] = default_cfg['SMD'][other_key]

    #pprint(cfg)
    return cfg, config_text
    
#
# This is the default configuration for the jig generator tool,
# and are chosen to be useful defaults that can reliably work
# for most users. Naturally, they won't necessarily be optimal
# for individual setups. With time, we can keep tuned values
# for specific common cases (e.g. 3D printers, components, etc)
# in the repository.
#
# Keep this well commented, and current.  This will help tool
# users understand and tune.
#
def get_default():
    return '''
# All dimensions are specified in millimeters
#
# Please see documentation for meaning of "gap", "overlap", and "perimeter"
#
[pcb]
thickness = 1.6
tesellate_edge_cuts_curve = 0.1

[holder]
# PCB holder holds both the PCB and components in place, in proper alignment
# This allows the user to use both hands while soldering, achieving the
# highest quality results.

# PCB rests at the top of the PCB holder. "pcb_overlap" decides how much
# plastic will be under the PCB on its edges, supporting it on the holder.
# A small overlap is enough to ensure that the PCB won't fall into the jig.
pcb_overlap = 0.3

# PCBs have an xy dimension accuracy of manufacturing, which shall be
# specified by the manufacturer. Beyond that, you also need to consider the
# printing accuracy of the 3D printer. "pcb_gap" ensures the PCB can be
# placed in the jig comfortably, without flexing the jig in any dimension.
pcb_gap = 0.3

# Drawings on the EdgeCuts layer provide the PCB outline. At every vertex
# on the PCB outline, a "lip" is created, to hold the PCB. Lips are created
# at these points:
#  - start end points of lines
#  - corners of rectangles
#  - start, mid and end points of arcs
#  - north, east, west, south points on circles
#  - each vertex on a polygon
#
# lips are generated only on the external edge of the PCB, i.e. they are not
# generated on interior drawings such as holes.
#
# Use 0 to enforce a lip along the entire PCB edge
#
lip_size = 15

# In some cases, you may want to force addition of lips at specific points.
#Use this. Note lip will be centerd on this point.
forced_lips = [
  #  [ X, Y ] coordinates from KiCAD PCB
]

# Holder will have a solid border around the edge points of the PCB. This
# provides rigidity to the jig structure.
perimeter = 1.6

[holder.base]
# Holder has a base. The base provides structural rigidity, and can be
# used for purposes such as annotation.

# Type of the base can be
# - "mesh". This is a sparse structure built of thick lines. This helps
#   reduce plastic usage, and can reduce print time. This also improves
#   visibility of components when they are placed in the jig.
# - "solid". A flat plate. More space for annotation.
type = "mesh"

# Thickness of the base. Higher value will improve rigidity in the
# xy dimension (apart from the lips)
thickness = 1

# A "perimeter" can be added on top of the base. This also improves the
# rigidity of the structure.
#
# Note that this dimension is additional to the base thickness.
perimeter_height = 2

[holder.base.mesh]
# Lines of the mesh are generated with this width. Best to use at-least 4
# times your nozzle thickness, if 3D printing. Thicker lines will use
# more material and increase printing time.
line_width = 2.0

# Height of the lines. If the base is solid, and height > thickness of the
# base, then they will protrude above the base.  In many cases, you can
# consider a thin base with lines providing extra structural strength
line_height = 1.0

[TH]
# Parameters for Through Hole processing

refs_do_not_process = [
  # list of refs that we should ignore
]
refs_process_only_these = [
  # list of refs to process
  # this takes precedence over "do_not_process"
  # this is exclusive with do_not_process
]

[TH.component_shell]
# Around each through hole component (ref), the jig generator creates a "shell"
# that serves as a component holder at its exact location on the board.

# shell can have one of a few types
# - wiggle    => A shape that gives a bit of wiggle room for the component,
#                when inserted into the shell. Depending on the exact shape of
#                the component, it may be possible to rock/shake the component
#                around.
# - fitting   => multiple outlines, like a "step well". Each level helps hold
#                the component in place, thus reducing wiggle room
# - tight     => step-well of concave hulls. Provides the tightest fit, but
#                also requires the most accuracy in dimensions and printing
# - courtyard => the "courtyard" of the component is used as the shape of the
#                shell. In almost all cases, this will allow the component
#                to move around freely in the shell. This is potentially
#                useful in two cases:
#                  - components that you mount on the PCB directly, rather
#                    than in the shell
#                  - With component_insertion="bottom" (see below), this
#                    gives ample room to push in the component
#
# "fitting" and "tight" are not implemented yet, and aren't treated valid
# right now.
type = "wiggle"

# component will typically be inserted from the top side (w.r.t # the PCB, and
# the jig). However, they can also be inserted from the bottom of the jig.
# bottom insertion means that base will have a hole to allow the component to
# be inserted. The shell type is forced to "outline" in this case.
# valid values : "top" or "bottom"
component_insertion = "top"

# Shells are basically a skin of plastic around the component, of this
# minimum thickness along the Z axis.
thickness = 1.2

# You a small xy gap for each component to slide into the shell, and it must
# ideally sit snug without moving. Component sizes have tolerance. So does
# you 3D. Consider both to set this up.
gap = 0.1

# If you have SMD components on the board, you may need a gap between the
# shells and the PCB. The gap ensures SMD components don't touch the shells.
#
# Think of this as a vertical "keep-out" distance between the PCB and the
# shells
clearance_from_pcb = 1

[SMD]
# Parameters for SMD components

# Shells must not touch SMD components. It is better to have a small clearance
# SMD keepout volume is it's courtyard extended along the height of the
# component, extended by "clearance_from_shells"
clearance_from_shells = 0.5

# "gap" is similar to clearance but in XY direction
# Courtyard is typically well outside the pads, so 0.5 mm is a good enough
# default. Soldered components will stay well within this.
gap_from_shells = 0.5

[jig]
#
# Jigs of various types can be generated:
#
# - "TH_soldering" creates a jig to help solder
#   through hole (TH) components. This creates
#   the PCB holder, the base, and the shells
#   for all components.
#
# - "component_fitting" creates only shells,
#   and the base, without creating the holder.
#
type = "TH_soldering"

# NOTE:
#
# Many of the parameters here map to OpenSCAD, and can be tuned there.
# E.g., parameters that are related to printing/manufacturing tolerances can be
# tuned in OpenSCAD, without access to the original PCB file.
#
# Parameters that result in geometry generation in the tool (e.g. lips)
# aren't tunable from OpenSCAD. Also, the shapes of the shells aren't
# tunable from OpenSCAD as parameters, but thickness and height can be
# easily changed. Please tune parameters carefully, and always 
# cross check generated models before printing/manufacturing.
#
'''
