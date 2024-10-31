#!/usr/bin/env python3

#
# Usage notes:
#
# Before running the script,
# 1. Export environment variables that are used by KiCAD
#    e.g. export KICAD8_3DMODEL_DIR=/usr/share/kicad/3dmodels
# 2. Ensure runtime dependencies are met.  Meshlab and
#    KiCAD are used for file format conversion at this time.
#    Without them, the script will fail
#

# Standard imports
import argparse
import os
import functools
from pprint import pprint
import numpy as np
import sys
import json
import subprocess
import tempfile
import re
import copy

# Local imports
from jigcommon import *
import edge_cuts
import mesh_ops
from solid2_module import module, exportReturnValueAsModule
import jigconfig
import tripy

# These are all modules you need to have
try:
    import tinyobjloader
    import pcbnew
    import solid2
    import toml
    import shapely
except ImportError as err:
    print(f'Missing module : {err.name}', file=sys.stderr)
    print('Please install it using pip, and retry.')
    sys.exit(1)

import scipy.spatial
from scipy.spatial.transform import Rotation
from solid2 import * # SolidPython
import solid2
import toml
from shapely.geometry import Polygon


def get_courtyard_polygon(shape):
    # Courtyard Polygon Coordinate System Note:
    #
    # KiCAD returned courtyard polygons are in the KiCAD coordinate system. No need to apply
    # offset or rotation. Just negate Y

    # Shree : Grr.. how do I extract the freakin points.
    # No API for get polygon ? Looks like only set stuff works from Python,
    # but gets are only in C. Find this hard to believe...
    # FIXME find out what you don't know!
    #shape.Format(False) gives a C/C++ source code representation !!
    #---------
    #SHAPE_LINE_CHAIN poly;
    #auto tmp = SHAPE_LINE_CHAIN( { VECTOR2I( 215000000, 105500000), VECTOR2I( 246000000, 88000000), VECTOR2I( 260500000, 118500000)}, true );;
    #poly.AddOutline(tmp); }
    #---------
    shapeText = shape.Format(False)
    verts = []
    for s_pt in re.findall('VECTOR2I\\( [0-9]+, [0-9]+\\)',shapeText):
        coord_parts = s_pt.split(' ')[1:]
        x = units_to_mm(int(coord_parts[0][:-1]))
        y = -units_to_mm(int(coord_parts[1][:-1]))
        verts.append([x,y]) # YES, and yikes!
    return verts

def get_ref_info(board):
    mounting_holes = []
    fp_list = board.Footprints()
    ref_map = {}
    fp_map = {}
    for fp in fp_list:
        ref = fp.GetReference()
        fp_x = units_to_mm(fp.GetX())
        fp_y = -units_to_mm(fp.GetY())
        #print(fp.GetReference())
        #print('  Position(mm):', fp_x, fp_y)
        #print('  On side     :', fp.GetSide())
        #print('  Orientation :', fp.GetOrientation().AsDegrees())
        #print('  DNP ?       :', fp.IsDNP())
        #print('  TH ?        :', fp.HasThroughHolePads())
        models_3d = []
        for mod3d in fp.Models():
            # NOTE XXX don't hold references to internal vectors - they can get
            # messed up! Make copies!
            models_3d.append({
                'model'    : mod3d.m_Filename,
                'offset'   : [mod3d.m_Offset[0], mod3d.m_Offset[1], mod3d.m_Offset[2]],
                'scale'    : [mod3d.m_Scale[0], mod3d.m_Scale[1], mod3d.m_Scale[2]],
                'rotation' : [mod3d.m_Rotation[0], mod3d.m_Rotation[1], mod3d.m_Rotation[2]]
            })
        fp_rot = fp.GetOrientation().AsDegrees()
        fields = fp.Fields()
        footprint = fields[2].GetText() # e.g. Package_QFP:LQFP-128_14x14mm_P0.4mm
        ref_info = {
            'is_th' : fp.HasThroughHolePads(),
            'footprint' : footprint,
            'x' : fp_x,
            'y' : fp_y,
            'orientation' : fp_rot,
            'side' : fp.GetSide(),
            'position' : fp.GetPosition(),
            'models' : models_3d,
            'kfp' : fp, # Reference if we need more info later
            'front_courtyard' : get_courtyard_polygon(fp.GetCourtyard(pcbnew.F_CrtYd)),
            'back_courtyard' : get_courtyard_polygon(fp.GetCourtyard(pcbnew.B_CrtYd)),
        }

        is_mounting_hole = False
        if fp.HasThroughHolePads():
            for fn in fp.Fields():
                if fn.GetText().startswith('MountingHole'):
                    mounting_holes.append(
                        [fp_x, fp_y]
                    )
                    is_mounting_hole = True
                    break
        ref_map[ref] = ref_info

        # remember that this footprint has this ref
        if not is_mounting_hole:
            if footprint in fp_map:
                fp_map[footprint]['refs'].append(ref)
            else:
                fp_map[footprint] = {
                    'is_th' : fp.HasThroughHolePads(),
                    'refs' : [ref],
                    'alias' : None,
                    'display_name' : None,
                    'force_smd' : False
                }


        #print(fp.Footprint().GetName())
        #pprint(dir(fp.Footprint()))
    return ref_map, fp_map, mounting_holes

# generate module names used in oscad
def ref2outline(ref):
    return 'ref_%s'%(ref)
def ref2wiggle_pocket(ref):
    return 'wiggle_pocket_%s'%(ref)
def ref2fitting_pocket(ref):
    return 'fitting_pocket_%s'%(ref)
def ref2perimeter(ref):
    return 'perimeter_%s'%(ref)

def ref2courtyard(ref):
    return 'courtyard_%s'%(ref)
def ref2courtyard_pocket(ref):
    return 'courtyard_pocket_%s'%(ref)
def ref2courtyard_perimeter(ref):
    return 'courtyard_perimeter_%s'%(ref)

def ref2keepout(ref):
    return 'keepout_%s'%(ref)

def expand_small_hole(poly_verts, area=None):
    if area is None:
        tris = tripy.earclip(poly_verts)
        area = tripy.calculate_total_area(tris)
    printable_threshold = cfg['3dprinter']['min_printable_hole_area']
    if area < printable_threshold:
        spoly = Polygon(poly_verts)
        # search in 0.01 mm offset increments
        # this helps achieve a good fit with the minimum
        # Note here we are not considering the gap that will be applied
        # additionally while generating the shell, so we are guaranteed to
        # be always higher than the safe limit
        search_steps = 10
        for offset in range(search_steps):
            offset_val = (offset+1)/search_steps
            offset_poly = spoly.buffer(offset_val)
            if offset_poly.area >= printable_threshold:
                print(f'  offset = {offset_val} improved area to {offset_poly.area} from {spoly.area}')
                poly_verts = offset_poly.exterior.coords
                break
    return poly_verts
mod_map = {}
wiggle_pocket_map = {}
fitting_pocket_map = {}
perimeter_map = {}

courtyard_map = {}
courtyard_pocket_map = {}
courtyard_perimeter_map = {}

keepout_map = {}

# sv => scad value
sv_max_z = {}
sv_shell_clearance = ScadValue('Shell_Clearance_From_PCB');
sv_shell_thickness = ScadValue('Shell_Thickness');
sv_shell_gap = ScadValue('Shell_Gap');
sv_pcb_thickness = ScadValue('PCB_Thickness');
sv_topmost_z = ScadValue('topmost_z')
sv_pcb_holder_perimeter = ScadValue('PCB_Holder_Perimeter')
sv_pcb_gap = ScadValue('PCB_Gap')
sv_pcb_overlap = ScadValue('PCB_Overlap')
sv_pcb_perimeter_height = ScadValue('Lower_Perimeter_Height')
sv_base_thickness = ScadValue('Base_Thickness')
sv_base_line_width = ScadValue('Base_Line_Width')
sv_base_line_height = ScadValue('Base_Line_Height')
sv_mesh_start_z = ScadValue('mesh_start_z')
sv_smd_clearance_from_shells = ScadValue('SMD_Clearance_From_Shells')
sv_smd_gap_from_shells = ScadValue('SMD_Gap_From_Shells')
def gen_fitting_pockets(mesh, z_bin_size):
    all_z = np.unique(mesh[:,2])
    all_z.sort()
    # we start working from highest Z (farthest from board)
    # and compute the "step-wells", i.e. convex hulls
    reverse_z = all_z[::-1]

    z_bins = []
    z_bins.append({
        "start_z" : reverse_z[0],
        "z_list" : [],
        "verts" : None
    })

    for z_val in reverse_z:
        # find all the verts at this Z
        mask = np.isin(element = mesh[:,2], test_elements=z_val)
        result = mesh[mask]

        # append to existing bin, if we belong to the same bin,
        # else start a new bin
        last_z_bin = z_bins[-1]["start_z"]
        if last_z_bin-z_val < z_bin_size:
            if z_bins[-1]["verts"] is None:
                z_bins[-1]["verts"] = result
            else:
                z_bins[-1]["verts"] = np.concatenate([z_bins[-1]["verts"], result])
            z_bins[-1]["z_list"].append(z_val)
        else:
            z_bins.append({
                "start_z" : z_val,
                "z_list" : [z_val],
                "verts" : result
            })

    # Each bin basically represents an area that casts a shadow looking down at Z=0
    # We can take the convex hull of the area.
    # Subsequent bins will keep adding to convex hulls from the previous bins
    # Thus, cumulatively we'll cover every bin, and the largest covex hull will be
    # at the bottom, allowing the part to slide in.

    prev_bin = None
    hull_bins = []
    for this_bin in z_bins:
        points_xy = this_bin['verts'][:,0:2] # chuck Z
        if len(hull_bins)>0:
            points_xy = np.concatenate([points_xy, hull_bins[-1]['hull']])
        hull = scipy.spatial.ConvexHull(points_xy)
        hull_verts = points_xy[hull.vertices]
        tris = tripy.earclip(hull_verts)
        area = tripy.calculate_total_area(tris)

        hull_verts = expand_small_hole(hull_verts, area)
        if len(hull_bins)>0:
            if area > hull_bins[-1]['area']+1: # heh - looking for some meaningful change :)
                # start new bin
                hull_bins.append({
                    'hull' : hull_verts,
                    'area' : area,
                    'z_list' : this_bin['z_list']
                })
            else:
                hull_bins[-1]['z_list'] = hull_bins[-1]['z_list']+this_bin['z_list']
        else:
            hull_bins.append({
                'hull' : hull_verts,
                'area' : area,
                "z_list" : this_bin["z_list"],
            })

    # reverse the hull bins, as it represents the right order of
    # cutting out!
    hull_bins.reverse()

    for idx, h_bin in enumerate(hull_bins):
        h_bin['z_list'].sort()
        if idx==0:
            h_bin['start_z'] = h_bin['z_list'][0]
        else:
            h_bin['start_z'] = hull_bins[idx-1]['z_list'][-1]
        h_bin['end_z'] = h_bin['z_list'][-1]

    # Each bin has start_z, end_z, area, hull and z_list keys
    return hull_bins

def extract_corners_2D(poly_points, anglular_thresh=10.0):
    """ Input is a 2D polygon. This function extracts "corners"
        in the polygon.  A corner is basically where two consective
        line segments have an angle more than the specified threshold """

    # FIXME: implement this - for now lazily return all points!
    return copy.deepcopy(poly_points)

def gen_shell_shape(ref, ident, x, y, rot, min_z, max_z, mesh):
    sv_tiny_dimension = ScadValue('tiny_dimension')
    sv_ref_shell_gap = ScadValue('Effective_Shell_Gap_For_%s'%(ref))
    sv_ref_shell_thickness = ScadValue('Effective_Shell_Thickness_For_%s'%(ref))
    sv_ref_shell_clearance = ScadValue('Effective_Shell_Clearance_From_PCB_For_%s'%(ref))
    sv_ref_max_z = ScadValue('max_z_%s'%(ref))
    sv_ref_min_z = ScadValue('min_z_%s'%(ref))

    h_bins = gen_fitting_pockets(mesh, 0.5)
    for this_bin in h_bins:
        this_bin['corners'] = extract_corners_2D(this_bin['hull'])

    # compute the fitting pockets
    fitting_pocket_name = ref2fitting_pocket(ident)
    fitting_pocket = union()
    min_fitting_z = h_bins[0]['start_z']

    corners = [ ]
    c_dia = cfg['3dprinter']['corner_dia']

    # we walk bins from inner (deeper) to outer (shallow)
    for this_bin in reversed(h_bins):
        pshape = polygon(this_bin['hull'])
        if c_dia > 0:
            # expand corners from previous iterations. these will impact the
            # "perimeter"
            for pt in corners:
                pt['dia'] += sv_ref_shell_gap + sv_ref_shell_thickness

            # include corners from this iteration
            # essentially, this ensures that the nozzle can't take a
            # "short cut" at corners, and has to trace a slight circle
            # due to this, plastic is absolutely avoided at the keepout
            # volumes with FDM printing
            bin_corners = []
            for pt in this_bin['corners']:
                new_point = { 'dia' : c_dia, 'loc' : pt }
                bin_corners.append(new_point)

            # union all the corner circles
            for c in bin_corners:
                loc = c['loc']
                pshape += translate([loc[0], loc[1], 0]) ( circle(d=c['dia']) )

            corners = corners + bin_corners
        # tiny_dimension ensures overlap across adjacent shells - important for boolean ops
        fitting_pocket += translate([0,0,-sv_tiny_dimension+min_fitting_z+(this_bin['start_z']-min_fitting_z)]) (
                            translate([x,y,sv_pcb_thickness]) (
                                linear_extrude(this_bin['end_z']-this_bin['start_z']+2*sv_tiny_dimension) (
                                    offset(sv_ref_shell_gap) (
                                        pshape
                                    )
                                )
                            )
                        )
    fitting_pocket_map[ident] = module(fitting_pocket_name, fitting_pocket)

    # outer outline will include all the corners circles!
    # the inner ones will probably (automatically) not contribute to the
    # external shape
    corner_fixes = union()
    for c in corners:
        loc = c['loc']
        corner_fixes += translate([loc[0], loc[1], 0]) ( circle(d=c['dia']) )

    # define the polygon so that we can do offset on it
    mod_name = ref2outline(ident)
    mod_map[ident] = module(mod_name, polygon(h_bins[0]['hull'])+corner_fixes)

    wiggle_pocket_name = ref2wiggle_pocket(ident)
    wiggle_pocket = translate([x,y,sv_pcb_thickness])(
                translate([0,0,sv_ref_min_z])(
                    linear_extrude(sv_ref_max_z-sv_ref_min_z) (
                        offset(sv_ref_shell_gap) (
                            mod_map[ident]()
                        )
                    )
                )
             )
    wiggle_pocket_map[ident] = module(wiggle_pocket_name, wiggle_pocket)

    perimeter_name = ref2perimeter(ident)
    perimeter_solid = translate([x,y,sv_pcb_thickness+sv_ref_shell_clearance]) (
                        linear_extrude(sv_topmost_z-sv_ref_shell_clearance+sv_base_thickness) (
                            offset(sv_ref_shell_gap+sv_ref_shell_thickness) (
                                mod_map[ident]()
                            )
                        )
                      )
    perimeter_map[ident] = module(perimeter_name, perimeter_solid,
                           comment=f"Perimeter for {ident}")

def gen_courtyard_shell_shape(ref, courtyard_poly):
    sv_ref_max_z = ScadValue('max_z_%s'%(ref))
    sv_ref_min_z = ScadValue('min_z_%s'%(ref))
    sv_ref_shell_gap = ScadValue('Effective_Shell_Gap_For_%s'%(ref))
    sv_ref_shell_thickness = ScadValue('Effective_Shell_Thickness_For_%s'%(ref))
    sv_ref_shell_clearance = ScadValue('Effective_Shell_Clearance_From_PCB_For_%s'%(ref))

    # a courtyard is a shape with a lot of wiggle room. So we don't do the corner
    # avoidance that is done in the wiggle/fitting shells
    courtyard_name = ref2courtyard(ref)
    courtyard_map[ref] = module(courtyard_name, polygon(courtyard_poly))

    courtyard_pocket_name = ref2courtyard_pocket(ref)
    courtyard_pocket = translate([0,0,sv_pcb_thickness]) (
                           translate([0,0,sv_ref_min_z]) (
                               linear_extrude(sv_ref_max_z-sv_ref_min_z) (
                                   offset(sv_ref_shell_gap) (
                                       courtyard_map[ref]()
                                   )
                               )
                           )
                       )
    courtyard_pocket_map[ref] = module(courtyard_pocket_name, courtyard_pocket)

    courtyard_perimeter_name = ref2courtyard_perimeter(ref)
    courtyard_perimeter_solid = translate([0,0,sv_pcb_thickness+sv_ref_shell_clearance]) (
                        linear_extrude(sv_topmost_z-sv_ref_shell_clearance+sv_base_thickness) (
                            offset(sv_ref_shell_gap+sv_ref_shell_thickness) (
                                courtyard_map[ref]()
                            )
                        )
                      )
    courtyard_perimeter_map[ref] = module(courtyard_perimeter_name, courtyard_perimeter_solid,
                           comment=f"Courtyard Perimeter for {ref}")

def gen_keepout_shape(ref, x, y, rot, min_z, max_z, courtyard_poly):
    sv_max_z[ref] = ScadValue('max_z_%s'%(ref))
    ref_smd_clearance_from_shells = ScadValue(f'smd_clearance_from_shells_{ref}')
    ref_smd_gap_from_shells = ScadValue(f'smd_gap_from_shells_{ref}')
    keepout_name = ref2keepout(ref)
    keepout_solid = translate([0,0,sv_pcb_thickness]) (
                     linear_extrude(sv_max_z[ref]+ref_smd_clearance_from_shells) (
                         offset(ref_smd_gap_from_shells) (
                            polygon(courtyard_poly)
                         )
                     )
                 )
    keepout_map[ref] = module(keepout_name, keepout_solid,
                           comment=f"Keepout volume for {ref}")
#
# Execution starts here
#

parser = argparse.ArgumentParser(
    description='Create jigs in a jiffy',
    epilog='Use the examples, Luke!')

group = parser.add_mutually_exclusive_group()
group.add_argument('--examples', default=False, action='store_true',
    help='''Shows examples of usage''')
group.add_argument('-i','--pcb', metavar='FILENAME',
    help='KiCAD PCB file (.kicad_pcb) to process')
group.add_argument('--footprint', metavar='SPEC',
    action='extend', nargs='+', type=str,
    help='''Generate a shell for specified kicad library footprint(s),
instead of a PCB. Use --examples for info on SPEC''')
group.add_argument('--model3d', action='extend', nargs='+', type=str,
    help='''Generate a shell from 3D model(s).''')

parser.add_argument('--config', metavar='FILENAME.toml',
    help='Use this configuration options file for various parameters.')

parser.add_argument('--pcb-footprints', type=bool, default=False,
    help='''Generates one shell per footprint used on the PCB design.''')
parser.add_argument('-o','--output',
    help='Output file.')
parser.add_argument('--output-format', default='stl', choices=['stl','scad'],
    help='Output file format')
parser.add_argument('--keep-orientation',
    action='store_true',
    default=False,
    help='''Match orientation of the output to KiCAD 3D view.
The default orients the output for direct printing (i.e. rotated by 180 degrees
along the X axis.)''')
parser.add_argument('--dump-config', metavar='FILENAME.toml',
    help='''Save a copy of the effective configuration after applying
    internal defaults, user and config file.''')
parser.add_argument('--genconfig', metavar='FILENAME.toml',
    help='''Generate a default configuration file, from an
    input KiCAD PCB. This lists all footprint and components.
    User friendly openscad output can be generated, starting
    with that as the base''')

group = parser.add_mutually_exclusive_group()
group.add_argument("-v", "--verbose", help='Verbose messages')
group.add_argument("-q", "--quiet", help='Info messages are suppressed')
args = parser.parse_args()

if args.examples:
    print(f"Sorry, examples aren't created yet. Use the source, Luke!", file=sys.stderr)
    sys.exit(1)
if args.footprint:
    print(f"Sorry, the footprint option isn't implemented yet", file=sys.stderr)
    sys.exit(1)

if args.model3d:
    print(f"Sorry, the model3d option isn't implemented yet", file=sys.stderr)
    sys.exit(1)

if not args.pcb:
    print(f'ERROR: Need a KiCAD PCB to work on. Use -h or --examples for more help', file=sys.stderr)
    sys.exit(1)

board = pcbnew.LoadBoard(args.pcb)
#smd_info, th_info, mounting_holes = get_ref_info(board)
ref_map, fp_map, mounting_holes = get_ref_info(board)

if args.genconfig:
    jigconfig.generate_config(args.genconfig, ref_map, fp_map)
    sys.exit(0)

jigconfig.load_user_config('jigify')

try:
    cfg, config_text, used_th_fp, th_ref_list, smd_ref_list = jigconfig.load(args.config, ref_map, fp_map)
except ValueError as err:
    print(f"ERROR: {err}", file=sys.stderr)
    sys.exit(-1)
#pprint(cfg)

if args.dump_config:
    with open(args.dump_config, 'w') as fp:
        toml.dump(cfg, fp)

if not args.output:
    print(f"ERROR: Need an output file", file=sys.stderr)
    sys.exit(-1)

pcb_thickness = cfg['pcb']['thickness']
shell_clearance = cfg['TH']['component_shell']['shell_clearance_from_pcb']
shell_type = cfg['TH']['component_shell']['shell_type']
if shell_type in ['tight']:
    print(f"ERROR: shell_type='{shell_type}' is not implemented yet.", file=sys.stderr)
    sys.exit(-1)
shell_gap = cfg['TH']['component_shell']['shell_gap']
shell_thickness = cfg['TH']['component_shell']['shell_thickness']

base_thickness = cfg['holder']['base']['thickness']
arc_resolution = cfg['pcb']['tesellate_edge_cuts_curve']

base_line_width = cfg['holder']['base']['line_width']
base_line_height = cfg['holder']['base']['line_height']
pcb_perimeter_height = cfg['holder']['base']['perimeter_height']
pcb_holder_gap = cfg['holder']['pcb_gap']
pcb_holder_overlap = cfg['holder']['pcb_overlap']
pcb_holder_perimeter = cfg['holder']['perimeter']
forced_pcb_supports = cfg['holder']['forced_grooves']
groove_size = cfg['holder']['groove_size']
ref_do_not_process = cfg['TH']['refs_do_not_process']
ref_process_only_these = cfg['TH']['refs_process_only_these']
jig_type = cfg['jig']['type']
jig_type_component_fitting = (jig_type == 'component_fitting')
smd_clearance_from_shells = cfg['SMD']['clearance_from_shells']
smd_gap_from_shells = cfg['SMD']['gap_from_shells']
if jig_type_component_fitting:
    if shell_clearance>0:
        print('INFO: Generating component shells, note shell_clearance=%s will cut into shell.'
            %(shell_clearance))

mounting_holes += forced_pcb_supports

# Setup environment for file name expansion
# First take from user config
for env_var_name in cfg['environment']:
    os.environ[env_var_name] = cfg['environment'][env_var_name]
os.environ["KIPRJMOD"] = os.path.split(args.pcb)[0]
# Now add onto it, this allows overrides from user config
path_sys_3dmodels = '/usr/share/kicad/3dmodels'
for ver in ['', 6,7,8]: # Hmm - would we need more ?
    env_var_name = 'KICAD%s_3DMODEL_DIR'%(ver)
    if env_var_name not in os.environ:
        os.environ[env_var_name] = path_sys_3dmodels

def load_3d_models(ref_list, ref_map, desc):
    fnames = []
    for ref in ref_list:
        comp = ref_map[ref]
        for modinfo in comp['models']:
            model_filename = os.path.expandvars(modinfo['model'])
            fnames.append(model_filename)
    # get uniques
    fnames = list(set(fnames))
    print(f'Loading {len(fnames)} 3D models for {desc} components...')
    for ref in ref_list:
        comp = ref_map[ref]
        for modinfo in comp['models']:
            model_filename = os.path.expandvars(modinfo['model'])
            modinfo['mesh'] = mesh_ops.load_mesh(model_filename)

load_3d_models(th_ref_list, ref_map, 'Through Hole')
load_3d_models(smd_ref_list, ref_map, 'SMD')

#pprint(th_info)

if args.output_format == 'stl':
    fp_scad = tempfile.NamedTemporaryFile(mode='w', suffix='.scad', delete_on_close=False)
    oscad_filename = fp_scad.name
    stl_filename = args.output
else:
    oscad_filename = args.output
    fp_scad = open(oscad_filename, 'w')

fp_scad.write('''// Customizable Jig Generator
// In OpenSCAD, use "Description Only" for best user experience
// understanding the tunable parameters.
// -----------------------------------------------------
// Auto generated file by jigify, the awesome automatic
// jig generator for your PCB designs.
// -----------------------------------------------------
// Input board file   : %s
// Configuration file : %s
//
// Complete configuration file is embedded at the end of this
// file.
'''%(args.pcb,
    '(Tool Default Internal Configuration)' if not args.config else args.config))

# We use OpenSCAD to do the grunt work of building the
# actual model for us. Tesellation level must be set
# to match or exceed the limits of 3D printing.
# 3D printing generally works on the order of 0.1mm
# so we choose a value half of that, 0.05 mm
# This should give a decent balance of smooth shapes
# and file sizes, and processing needs.
fp_scad.write("""
// { These are configurable parameters
// you can tweak these here and count on
// openscad magic to do show you the result
// right away!

/* [Preview Options - No effect on STL output] */
// Show PCB for reference (green)
Show_PCB = true; // [true,false]
// Transparency of PCB
PCB_Transparency = 0.5; //[0.0:1.0]

// Show component volumes (orange)
Show_Component_Volumes = true; // [true,false]
// Transparency of component volumes
Component_Volume_Transparency = 0.2; // [0.0:1.0]

// Show volume reserved to avoid touching SMD parts (red)
Show_SMD_Keepout_Volumes = true; // [true,false]
// Transparency of SMD keepout volumes
SMD_Keepout_Volume_Transparency = 0.4; // [0.0:1.0]

/* [PCB] */
PCB_Thickness=%s;

/* [Jig] */
Type_of_Jig = "%s"; // [TH_soldering,component_fitting]

/* [Through Hole Soldering Jig] */

// Gap between PCB edge and slot on the jig
PCB_Gap=%s;

// Width of the groove on the jig, holding the PCB
PCB_Overlap=%s;

// Wall thickness of the jig, abutting the PCB
PCB_Holder_Perimeter=%s;

// Height of solid perimeter at the base
Lower_Perimeter_Height = %s;

Groove="At PCB Corners: %s mm"; //["At PCB Corners: %s mm", "All Around PCB Edge"]

/* [Base] */

// Type of Base
Base_Type = "%s"; // [x_lines, y_lines, griddish, mesh, solid]

// Thickness of Base
Base_Thickness = %s;

// Width of Lines on Base
Base_Line_Width = %s;

// Height of Lines on Base
Base_Line_Height = %s;

/* [SMD Keepout] */

// SMD keepout volume extension in Z
SMD_Clearance_From_Shells=%s;

// SMD keepout volume extension in XY
SMD_Gap_From_Shells=%s;
"""%(
pcb_thickness,
jig_type,
pcb_holder_gap, pcb_holder_overlap, pcb_holder_perimeter, pcb_perimeter_height,
groove_size, groove_size,
cfg['holder']['base']['type'],
base_thickness,
base_line_width, base_line_height,
smd_clearance_from_shells,
smd_gap_from_shells
))

pcb_segments = []
pcb_filled_shapes = []
seg_shapes = []

edge_cuts.load(board, pcb_segments, pcb_filled_shapes)

seg_shapes = []
if not edge_cuts.coalesce_segments(pcb_segments, seg_shapes):
    print('ERROR: Please check the edge cuts layer in KiCAD.')
    print('ERROR: There are incomplete outlines. DRC or 3D View should help')
    print('ERROR: diagnose the issue')
    sys.exit(-1)

edge_cuts.tesellate(arc_resolution, seg_shapes, pcb_filled_shapes)
edge_cuts.compute_areas(pcb_filled_shapes)

if len(pcb_filled_shapes) == 0:
    print('ERROR: At-least one filled shape is needed in Edge.Cuts layer')
    print('Please check and validate board file.')
    sys.exit(-1)

# Find the largest filled area. This is assumed to be the actual
# PCB outline
pcb_filled_shapes.sort(key=lambda x:x['area'], reverse=True)
# And hence these are the vertices
pcb_edge_points = pcb_filled_shapes[0]['vertices']

def xform_mesh(mesh, modinfo, orientation):
    mesh_scale = mesh * modinfo['scale']
    # FIXME: Hrrmph! why should I need to reverse these
    # angles !?
    rx = Rotation.from_euler('x', -modinfo['rotation'][0], degrees=True)
    ry = Rotation.from_euler('y', -modinfo['rotation'][1], degrees=True)
    rz = Rotation.from_euler('z', -modinfo['rotation'][2], degrees=True)
    rot_angle=th['orientation'];
    rz2 = Rotation.from_euler('z', rot_angle, degrees=True)
    r_xyz = rx*ry*rz
    mesh_rotated = r_xyz.apply(mesh_scale)
    mesh2 = mesh_rotated + [modinfo['offset'][0], modinfo['offset'][1], modinfo['offset'][2]]
    mesh = rz2.apply(mesh2)
    min_z = min(mesh[:,2])
    max_z = max(mesh[:,2])
    return mesh, min_z, max_z

all_shells = []
fp_centers = []
topmost_z = 0

# For each TH component on the board
for this_ref in th_ref_list:
    print('Processing TH :', this_ref)
    # each footprint can have multiple models.
    # each model that is "in contact" with the board will generate
    # a shell
    local_max_z = 0
    local_min_z = float('inf')
    subshells = {
        'ref' : this_ref,
        'wiggle' : [],
        'courtyard' : None
    }
    th = ref_map[this_ref]
    for idx, modinfo in enumerate(th['models']):
        mesh = modinfo['mesh']
        if mesh.shape[0]==0:
            print('  WARNING: Mesh %s is empty on import. Watch out for intended side effects. SKIPPING!'
                  %(modinfo['model']))
            continue
        mesh, min_z, max_z = xform_mesh(mesh, modinfo, th['orientation'])
        #print('min_z = ', min_z, ' max_z = ', max_z)
        if min_z>0:
            print('  Mesh %s is NOT mounted on board. Skipping.'%(modinfo['model']))
        else:
            min_x = min(mesh[:,0])
            max_x = max(mesh[:,0])
            min_y = min(mesh[:,1])
            max_y = max(mesh[:,1])
            center_x = ((min_x + max_x)/2)
            center_y = ((min_y + max_y)/2)
            fp_centers.append([center_x+th['x'],center_y+th['y']])
            #print('Hull size = ', len(hull.vertices), ' min Z=', min_z, ' max Z=', max_z)
            #print(hull_verts)
            shell_ident = '%s_%d'%(this_ref,idx)
            gen_shell_shape(this_ref, shell_ident,
                    th['x'], th['y'], th['orientation'],
                    min_z, max_z, mesh)
            subshells['wiggle'].append({
                'name':shell_ident,
                'min_z':min_z,
                'max_z':max_z,
                'model':modinfo['model'],
                'x' : th['x'],
                'y' : th['y'],
                'orientation' : th['orientation'],
                'fp_center': [center_x+th['x'],center_y+th['y']]
            })
            print('  Generating shell %s for ref %s with mesh %s'%(shell_ident, this_ref, modinfo['model']))
            local_max_z = max(local_max_z, max_z)
            local_min_z = min(local_min_z, min_z)
    if local_max_z > 0: # Means we found a mesh
        subshells['max_z'] = local_max_z
        subshells['min_z'] = local_min_z
        gen_courtyard_shell_shape(this_ref, th['front_courtyard'])
        subshells['front_courtyard'] = th['front_courtyard']
        all_shells.append(subshells)
    topmost_z = max(topmost_z, local_max_z)

smd_keepouts = []
for this_ref in smd_ref_list:
    smd = ref_map[this_ref]
    #print('Processing SMD :', smd['ref'])
    # each footprint can have multiple models.
    # each model that is "in contact" with the board will generate
    # a shell
    for idx, modinfo in enumerate(smd['models']):
        mesh = modinfo['mesh']
        if mesh.shape[0]==0:
            print('  WARNING: Mesh %s is empty on import. Watch out for intended side effects. SKIPPING!'
                  %(modinfo['model']))
            continue
        mesh, min_z, max_z = xform_mesh(mesh, modinfo, smd['orientation'])
        keepout_ident = '%s_%d'%(this_ref,idx)
        gen_keepout_shape(keepout_ident,
            smd['x'], smd['y'], smd['orientation'],
            min_z, max_z, smd['front_courtyard'])
        smd_keepouts.append({
            'name' : keepout_ident,
            'ref' : this_ref,
            'min_z':min_z,
            'max_z':max_z,
            'model':modinfo['model'],
            'x' : smd['x'],
            'y' : smd['y'],
            'orientation' : smd['orientation'],
        })
        topmost_z = max(topmost_z, max_z)

bottom_insertion_z = topmost_z + 2*base_thickness

# Allow tweaking per component values from customizer
# Refs we get can be in any order. Customizer in OpenSCAD opens up all the tabs
# together. This creates a cluttered user interface. Question is how do we
# enable the user to easily find the components they may want to tweak ? A
# reasonable assumption we can make is - they are likely to need to tweak major
# components, rather than minor components.  Major components will be things
# such as connectors and ICs. We can show the larger components ahead of the
# rest, by rearranging the refs by area of the courtyard.
ui_refs = []
for subshells in all_shells:
    this_ref = subshells['ref']
    tris = tripy.earclip(subshells['front_courtyard'])
    area = tripy.calculate_total_area(tris)
    ui_refs.append([this_ref,area])
ui_refs.sort(reverse=True, key=lambda x:x[1]) # key is the area
#pprint(ui_refs)

fp_scad.write('/* [Include these components in output STL file] */\n')
for this_ref, area in ui_refs:
    footprint = cfg['TH'][this_ref]['kicad_footprint']
    dname_fp = fp_map[footprint]['display_name']
    dname_ref = cfg['TH'][this_ref]['display_name']
    if dname_ref==this_ref:
        fp_scad.write('//%s (%s)\n'%(dname_fp, this_ref))
    else:
        fp_scad.write('//%s ( %s, %s)\n'%(dname_ref, this_ref, dname_fp))
    fp_scad.write('Include_%s_in_Jig=true; // [false,true]\n'%(this_ref))

for alias in cfg['footprint']:
    footprint = cfg['footprint'][alias]
    fp_scad.write('/* [Footprint: %s] */\n'%(footprint['display_name']))
    var_prop_rem = [['Shell_Gap', 'shell_gap', 'XY Gap in shell for component insertion'],
               ['Shell_Thickness', 'shell_thickness', 'Thickness of shell'],
               ['Shell_Clearance_From_PCB', 'shell_clearance_from_pcb', 'Z distance from start of shell to PCB']]
    for var, prop, rem in var_prop_rem:
        fp_scad.write('//%s\n'%(rem))
        fp_scad.write('%s_For_%s = %s;\n'%(var, alias, footprint[prop]))

for this_ref, area in ui_refs:
    footprint = cfg['TH'][this_ref]['kicad_footprint']
    dname_fp = fp_map[footprint]['display_name']
    dname_ref = cfg['TH'][this_ref]['display_name']
    if dname_fp == dname_ref:
        fp_scad.write('/* [%s (%s)] */\n'%(this_ref, footprint))
    else:
        fp_scad.write('/* [%s( %s, %s )] */\n'%(dname_ref, this_ref, footprint))
    fp_scad.write('//Type of shell for this component\n')
    fp_scad.write('Shell_Type_For_%s="%s"; // [wiggle,fitting,courtyard]\n'%(
        this_ref,
        cfg['TH'][this_ref]['shell_type'])
    )
    fp_scad.write('//Insert this component into jig from this side.(Bottom insertion requires wiggle or courtyard shell to work)\n')
    fp_scad.write('Insert_%s_From="%s"; // [top,bottom]\n'%(
        this_ref,
        cfg['TH'][this_ref]['insertion_direction'])
    )
    fp_scad.write('//Delta(+/-) thickness for shell, additional to footprint setting\n')
    fp_scad.write('Delta_Shell_Thickness_For_%s=%s;\n'%(
        this_ref,
        cfg['TH'][this_ref]['delta_shell_thickness'])
    )
    fp_scad.write('//Delta XY gap to allow insertion of this component into its shell\n')
    fp_scad.write('Delta_Shell_Gap_For_%s=%s;\n'%(
        this_ref,
        cfg['TH'][this_ref]['delta_shell_gap'])
    )
    fp_scad.write('//Delta Z clearance from the shell to PCB\n')
    fp_scad.write('Delta_Shell_Clearance_From_PCB_For_%s=%s;\n'%(
        this_ref,
        cfg['TH'][this_ref]['delta_shell_clearance_from_pcb'])
    )

fp_scad.write('// } End of configurable parameters\n')

fp_scad.write('/* [Hidden] */\n')
fp_scad.write('$fs = 0.05;\n');

# Effective values for each ref
for this_ref, area in ui_refs:
    footprint = ref_map[this_ref]['footprint']
    alias = fp_map[footprint]['alias']
    for this_var in ['Shell_Thickness', 'Shell_Gap', 'Shell_Clearance_From_PCB']:
        fp_scad.write('Effective_%s_For_%s = %s_For_%s + Delta_%s_For_%s;\n'%(
            this_var, this_ref, this_var, alias, this_var, this_ref)
        )
fp_scad.write('''
// { START : Computed Values

// Height of the tallest component on the top side
topmost_z=%s;

groove_width = max(PCB_Gap+PCB_Holder_Perimeter, PCB_Overlap)*2.2;
tiny_dimension = 0.001;
base_z =  PCB_Thickness+topmost_z+Base_Thickness+2*tiny_dimension;

mesh_start_z = PCB_Thickness+topmost_z+Base_Thickness-Base_Line_Height;
'''%( topmost_z))


fp_scad.write('bottom_insertion_z = %s;\n'%(bottom_insertion_z))
fp_scad.write('// Height of the individual components\n')
for subshells in all_shells:
    this_ref = subshells['ref']
    applies_to = ''
    for shell_info in subshells['wiggle']:
        applies_to += ' %s'%(shell_info['model'])
    fp_scad.write('max_z_%s= (Insert_%s_From=="bottom")? bottom_insertion_z : %s; //Applies to 3D Model(s):%s\n'%(this_ref,this_ref, subshells['max_z'], applies_to))
    fp_scad.write('min_z_%s= %s;\n'%(this_ref, subshells['min_z']))
for keepout_info in smd_keepouts:
    fp_scad.write('max_z_%s= %s; //3D Model: %s\n'%(keepout_info['name'],keepout_info['max_z'], keepout_info['model']))
    ref = keepout_info['ref']
    fp_scad.write('smd_clearance_from_shells_%s= SMD_Clearance_From_Shells;\n'%(keepout_info['name']))
    fp_scad.write('smd_gap_from_shells_%s= SMD_Gap_From_Shells;\n'%(keepout_info['name']))
fp_scad.write('// } END : Computed Values\n')
fp_scad.write('\n')

# Write out the PCB edge
pcb_edge_points = np.array(pcb_edge_points)
pcb_edge_points[:,1] *= -1.0 # Negate all Ys to fix coordinate system
#hull = scipy.spatial.ConvexHull(pcb_edge_points)
#pcb_edge_hull = pcb_edge_points[hull.vertices]
sm_pcb_edge = module('pcb_edge', polygon(pcb_edge_points))
sm_pcb = module('pcb', linear_extrude(sv_pcb_thickness) (sm_pcb_edge()))
# Uncomment to see PCB edge as a 2D diagram :)
#from matplotlib import pyplot as plt
#x, y = pcb_edge_points.T
#plt.scatter(x1,y)
#plt.plot(x1,y1)
#plt.show()


# all SMD keepouts
combined_keepouts = union()
for keepout_info in smd_keepouts:
    combined_keepouts += keepout_map[keepout_info['name']]()
sm_mounted_smd_keepouts = module('mounted_smd_keepouts',
        combined_keepouts
    )
# Compute bounding box of PCB
# FIXME: make this min, max code less verbose
pcb_min_x = pcb_max_x = pcb_edge_points[0][0]
pcb_min_y = pcb_max_y = pcb_edge_points[0][1]
for pt in pcb_edge_points:
    pcb_min_x = min(pcb_min_x, pt[0])
    pcb_max_x = max(pcb_max_x, pt[0])
    pcb_min_y = min(pcb_min_y, pt[1])
    pcb_max_y = max(pcb_max_y, pt[1])

pcb_bb_corners = [
    [pcb_min_x , pcb_min_y],
    [pcb_min_x , pcb_max_y],
    [pcb_max_x , pcb_min_y],
    [pcb_max_x , pcb_max_y]
]

@exportReturnValueAsModule
def wide_line(start, end):
    return solid2.hull()(
            translate(start)(cylinder(h=sv_base_line_height, d=sv_base_line_width))+
            translate(end)(cylinder(h=sv_base_line_height, d=sv_base_line_width))
            )
# Delaunay triangulation will be done on the following points
# 1. centers of all considered footprints
# 2. mounting holes
# 3. bounding box corners of PCB edge. mounting holes are
#    inside the PCB and don't extend all the way to the edge.
#    If we don't include them, we may end up having a separate
#    "delaunay island", depending on the exact PCB.
dt_centers = fp_centers + mounting_holes + pcb_bb_corners

base_solid = translate([0,0,sv_pcb_thickness+sv_topmost_z])(
                linear_extrude(sv_base_thickness) (
                    offset(sv_pcb_holder_perimeter+sv_pcb_gap)(
                        sm_pcb_edge()
                    )
                )
             )
sm_base_solid = module('base_solid', base_solid)

mesh_lines = union()
if len(dt_centers)>=4:
    mesh_comment = 'delaunay triangulated mesh'
    d_verts = np.array(dt_centers)
    d_tris = scipy.spatial.Delaunay(d_verts)
    for tri in d_tris.simplices:
        # tri is a,b,c
        av = d_verts[tri[0]]
        a = [av[0], av[1]]
        bv = d_verts[tri[1]]
        b = [bv[0], bv[1]]
        cv = d_verts[tri[2]]
        c = [cv[0], cv[1]]
        mesh_lines += wide_line(a,b)
        mesh_lines += wide_line(b,c)
        mesh_lines += wide_line(c,a)
else:
    if len(dt_centers)>1:
        av = dt_centers[0]
        a = [av[0],av[1]]
        bv = dt_centers[1]
        b = [bv[0],bv[1]]
        mesh_lines += wide_line(a,b)
    if len(dt_centers)==3:
        cv = dt_centers[2]
        c = [cv[0],cv[1]]
        mesh_lines += wide_line(b,c)
        mesh_lines += wide_line(c,a)

base_mesh_volume = linear_extrude(sv_base_line_height) (
                       offset(sv_pcb_holder_perimeter+sv_pcb_gap) (
                           sm_pcb_edge()
                       )
                   )
sm_base_mesh_volume = module('base_volume', base_mesh_volume)

base_mesh = translate([0,0,sv_mesh_start_z]) (
                intersection() (
                    mesh_lines,
                    sm_base_mesh_volume()
                )
            )

sm_base_mesh = module('base_mesh', base_mesh())

pcb_holder = linear_extrude(sv_topmost_z+sv_pcb_thickness+sv_base_thickness) (
        difference()(
            offset(sv_pcb_holder_perimeter+sv_pcb_gap)(
                sm_pcb_edge()
            ),
            offset(sv_pcb_gap) (
                sm_pcb_edge()
            )
        )
    ) + translate([0,0,sv_pcb_thickness]) (
        linear_extrude(sv_topmost_z+sv_base_thickness) (
            difference() (
                offset(sv_pcb_gap)(
                    sm_pcb_edge()
                ),
                offset(-sv_pcb_overlap)(
                    sm_pcb_edge()
                )
            )
        )
    )
sm_pcb_holder = module('pcb_holder', pcb_holder,
        comment = 'solid between perimeter and outline, full height')

pcb_perimeter_short = translate([0,0,sv_pcb_thickness+sv_topmost_z-sv_pcb_perimeter_height]) (
        linear_extrude(sv_pcb_perimeter_height+sv_base_thickness) (
            difference() (
                offset(sv_pcb_holder_perimeter+sv_pcb_gap) (
                    sm_pcb_edge()
                ),
                offset(-sv_pcb_overlap)(
                    sm_pcb_edge()
                )
            )
        )
    )

sm_pcb_perimeter_short = module('pcb_perimeter_short', pcb_perimeter_short)

groove_lines = edge_cuts.compute_grooves(arc_resolution, pcb_filled_shapes[0], groove_size)
#print('groove lines no = ',len(groove_lines))
#pprint(groove_lines)
@exportReturnValueAsModule
def peri_line(start, end, line_width):
    return solid2.hull() (
        circle(d=line_width).translate(start),
        circle(d=line_width).translate(end)
    )

#fp_scad.write('  groove_width = max(pcb_gap+pcb_holder_perimeter, pcb_overlap)*1.2;\n')
#fp_scad.write('  tiny_dimension = 0.001;\n')
#fp_scad.write('  base_z =  pcb_thickness+topmost_z+base_thickness+2*tiny_dimension;\n')
sv_groove_width = ScadValue('groove_width')
sv_tiny_dimension = ScadValue('tiny_dimension')
sv_base_z = ScadValue('base_z')
s_groove_lines = union()
for line in groove_lines:
    # FIXME: see the -y below? This is ugliness. Aim for consistency
    s_groove_lines += peri_line(
                    [line[0][0],-line[0][1]],
                    [line[1][0],-line[1][1]],
                    sv_groove_width)
sm_pcb_support_groove = module('pcb_support_groove',
        translate([0,0,-sv_tiny_dimension]) (
            linear_extrude(sv_base_z) (
                s_groove_lines
            )
        )
)

# Write out entire SolidPython generated scad, including the modules
# Hack : ScadValue can't be empty - so passing it a comment!
fp_scad.write(scad_render(ScadValue('//\n')))

fp_scad.write('module mounted_component_perimeters() {\n')
fp_scad.write('  union() {\n')
for subshells in all_shells:
    this_ref = subshells['ref']
    fp_scad.write('  if(Include_%s_in_Jig) {\n'%(this_ref))
    fp_scad.write('  if(Shell_Type_For_%s=="courtyard") {\n'%(this_ref))
    fp_scad.write('      %s();\n'%(ref2courtyard_perimeter(this_ref)))
    fp_scad.write('} else {\n')
    for shell_info in subshells['wiggle']:
        this_name = shell_info['name']
        fp_scad.write('      %s();\n'%(ref2perimeter(this_name)))
    fp_scad.write('}\n')
    fp_scad.write('}\n') # included
fp_scad.write('  }\n')
fp_scad.write('}\n')

fp_scad.write('module mounted_component_pockets() {\n')
fp_scad.write('  union() {\n')
# NOTE: pockets are always included, unlike shells. This is to prevent
# protruding shells from elsewhere to get into that volume
for subshells in all_shells:
    this_ref = subshells['ref']
    fp_scad.write('  if(Shell_Type_For_%s=="courtyard") {\n'%(this_ref))
    fp_scad.write('      %s();\n'%(ref2courtyard_pocket(this_ref)))
    fp_scad.write('} else {\n')
    fp_scad.write('  if(Shell_Type_For_%s=="wiggle") {\n'%(this_ref))
    for shell_info in subshells['wiggle']:
        this_name = shell_info['name']
        fp_scad.write('      %s();\n'%(ref2wiggle_pocket(this_name)))
    fp_scad.write('  } else { // fitting\n')
    for shell_info in subshells['wiggle']:
        this_name = shell_info['name']
        fp_scad.write('      %s();\n'%(ref2fitting_pocket(this_name)))
    fp_scad.write('  }\n')
    fp_scad.write('}\n')
fp_scad.write('  }\n')
fp_scad.write('}\n')

fp_scad.write('module base_x_lines() {\n')
fp_scad.write('  translate([0,0,mesh_start_z]) {\n')
fp_scad.write('    intersection() {\n')
fp_scad.write('      union() {\n')
for subshells in all_shells:
    this_ref = subshells['ref']
    fp_scad.write('        if(Include_%s_in_Jig) {\n'%(this_ref))
    for shell_info in subshells['wiggle']:
        ref_x, ref_y = shell_info['fp_center']
        h_start = '[pcb_min_x, %s]'%(ref_y)
        h_end = '[pcb_max_x, %s]'%(ref_y)
        fp_scad.write('          wide_line(%s,%s);\n'%(h_start,h_end))
    fp_scad.write('        }\n')
fp_scad.write('      }\n')
fp_scad.write('      base_volume();\n')
fp_scad.write('    }\n')
fp_scad.write('  }\n')
fp_scad.write('}\n')

fp_scad.write('module base_y_lines() {\n')
fp_scad.write('  translate([0,0,mesh_start_z]) {\n')
fp_scad.write('    intersection() {\n')
fp_scad.write('      union() {\n')
for subshells in all_shells:
    this_ref = subshells['ref']
    fp_scad.write('        if(Include_%s_in_Jig) {\n'%(this_ref))
    for shell_info in subshells['wiggle']:
        ref_x, ref_y = shell_info['fp_center']
        v_start = '[%s, pcb_min_y]'%(ref_x)
        v_end = '[%s, pcb_max_y]'%(ref_x)
        fp_scad.write('          wide_line(%s,%s);\n'%(v_start,v_end))
    fp_scad.write('        }\n')
fp_scad.write('      }\n')
fp_scad.write('      base_volume();\n')
fp_scad.write('    }\n')
fp_scad.write('  }\n')
fp_scad.write('}\n')

fp_scad.write('module base_griddish() {\n')
fp_scad.write('  union() {\n')
fp_scad.write('    base_x_lines();\n')
fp_scad.write('    base_y_lines();\n')
fp_scad.write('  }\n')
fp_scad.write('}\n')

fp_scad.write('''

module preview_helpers() {
  if(Show_PCB) {
    // Show transparent PCB. We use the background modifier, so this
    // won't be in output
    color("darkgreen", 1.0-PCB_Transparency) {
      %pcb();
    }
  }
  
  if(Show_Component_Volumes) {
    color("darkorange", 1.0-Component_Volume_Transparency) {
      %mounted_component_pockets(); // always include, but don't visualize
    }
  }
  
  if(Show_SMD_Keepout_Volumes) {
    color("crimson", 1.0-SMD_Keepout_Volume_Transparency) {
      %mounted_smd_keepouts();
    }
  }
}

// This _is_ the entire jig model. Structured such that
// you can individually check the parts. Color codes from
// https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Transformations#color
module complete_model_TH_soldering() {
  color("steelblue") {
    difference() {
      union() {
        if(Groove == "All Around PCB Edge") {
          pcb_holder();
        } else {
          intersection() {
            pcb_support_groove();
            pcb_holder();
          };
        }
        pcb_perimeter_short();
        if(Base_Type=="mesh") {
          base_mesh();
        } else if(Base_Type=="griddish") {
          base_griddish();
        } else if(Base_Type=="x_lines") {
          base_x_lines();
        } else if(Base_Type=="y_lines") {
          base_y_lines();
        } else {
          base_solid();
        }
        mounted_component_perimeters();
      }
      mounted_component_pockets(); // FIXME: fix terminology - "included"
      mounted_smd_keepouts();
    }
  }
  preview_helpers();
}
module complete_model_component_fitting() {
  color("steelblue") {
    difference() {
      union() {
        if(Base_Type=="mesh") {
          base_mesh();
        } else if(Base_Type=="griddish") {
          base_griddish();
        } else if(Base_Type=="x_lines") {
          base_x_lines();
        } else if(Base_Type=="y_lines") {
          base_y_lines();
        } else {
          base_solid();
        }
        mounted_component_perimeters();
      }
      mounted_component_pockets(); // FIXME: fix terminology - "included"
      mounted_smd_keepouts();
    }
  }
  preview_helpers();
}
''')

fp_scad.write('''
pcb_min_x = %s;
pcb_max_x = %s;
pcb_min_y = %s;
pcb_max_y = %s;
'''%(pcb_min_x, pcb_max_x, pcb_min_y, pcb_max_y))

fp_scad.write('''
module style_of_jig() {
  if(Type_of_Jig=="TH_soldering")
    complete_model_TH_soldering();
  else
    complete_model_component_fitting();
}

orient_to_print=%d;
if(orient_to_print == 1) {
  // Center the PCB around the origin in XY,
  // This helps interaction with OpenSCAD
  translate([
    -pcb_min_x-0.5*(pcb_max_x-pcb_min_x),
    pcb_min_y+0.5*(pcb_max_y-pcb_min_y), 0 ]) {
    rotate([180,0,0]) {
        style_of_jig();
    }
  }
} else {
    style_of_jig();
}
'''%(not args.keep_orientation))

fp_scad.write('''
/*
%s
*/
'''%(config_text))

fp_scad.close()

if args.output_format == 'stl':
    cmd = []
    # FIXME - ideally we'll have a config in ~/.config to store things
    # like paths to binaries etc
    cfg_scad = cfg['openscad']
    cmd += [ os.path.expanduser(cfg_scad['binary']),
             '--hardwarnings' ]
    if cfg_scad['use_manifold']:
        cmd += [ '--backend', 'Manifold' ]
    cmd += [ '-o', stl_filename, oscad_filename ]
    print('Generating output using : %s'%(' '.join(cmd)))
    print('-----------------------------------------')
    retcode = subprocess.call(cmd)
    print('-----------------------------------------')
    if retcode !=0 :
        print('ERROR: OpenSCAD Failed, exit code %d'%(retcode))
    else:
        print('Done, output : %s'%(stl_filename))
else:
    print('Done, output : %s'%(oscad_filename))
#
#
# Coordinate system notes:
#
# We adhere to the normal 3D coordinate system in this program,
# with Z pointing up.
#
# KiCAD uses a coordinate system where X increases to the right,
# and Y increases down - like a regular framebuffer. Therefore,
# Y coordinates from KiCAD needs to be negated to map to the
# regular 3D system. Note that 3D meshes in KiCAD use the regular
# 3D coordinate system, so those coordinates don't need to be
# transformed.
#
# This script also uses OpenSCAD. The extrude operation extrudes
# along the positive Z axis - i.e. upwards.
#
# Z = 0 corresponds to the bottom of the PCB. At the top of the
# PCB, Z = PCB thickness
#
# This setup is so that we can exactly match KiCAD's step file
# export of the board. Overlaying the mesh generated on this
# program on top of the step file is useful both for debugging
# as well as understanding any issues.
#
# Here is the Z coordinate stackup, for top side assembly.
# Soldering is on the bottom side.
#
# Z = topmost + 1 | "base". Start of 1 mm thick layer, delaunay triangles
#     +thickness  |
#
# Z = topmost     | topmost point of tallest component, when mounted on
#     +thickness | the PCB. Typically the long end of a berg header
#
# Z = in-between  | Highest point of intermediate height component
#
# Z = thickness+  | Start of typical shell
#     clearance   | Clearance allows user to visually verify component
#                 | placement and fit from the sides
#
# Z = thickness   | PCB top
#
# Z = 0           | PCB botto
#
# This program uses this terminology
#
# edge      => closed polygon matching exact border.
#              e.g. the outer edge of the PCB. In the case of a component
#              you can think of a projection of the 3d model of
#              the component onto the Z plane, and the resulting outline.
#              outlines can be concave.
#
# hull      => convex hull of the edge. Typically used as these are
#              easier to compute and have useful properties. Using
#              concave surfaces needs more care, else things may break.
#
# overlap   => small inset of the edge/hull.
#              offset value here is called "overlap" as well.
#
# outline    => small offset of the edge/hull (meaning outwards)
#               offset value here is called "gap"
#
# perimeter => large offset of the edge/hull. Typically used to
#              build "walls" or shells
#              offset value here is termed "thickness"
#
# component/board can slide inside outline, but will abut above an
# overlap. The term "clearance" is used to a gap in the
# Z direction.
#
# "pocket" is a negative shape - e.g. the hollow cavity required
# to push in a component
#
# "shell" is a solid shape with a cavity. the cavity is a "pocket" so
# that the component can be held in the pocket.
#
#