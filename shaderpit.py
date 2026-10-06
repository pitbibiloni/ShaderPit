ML_GRUNGE_SYNCING = False

bl_info = {
    "name": "ShaderPit V0.9.79",
    "author": "Pit Bibiloni",
    "version": (0, 9, 79),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > ShaderPit",
    "description": "Material variation and look-development tools for Blender.",
    "category": "Material",
}

import bpy
import os
import random
import json
from bpy.types import Operator, Panel, PropertyGroup, UIList, AddonPreferences
from bpy.props import StringProperty, IntProperty, FloatProperty, FloatVectorProperty, PointerProperty, BoolProperty, EnumProperty, CollectionProperty


# ------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------


def _addon_preferences():
    try:
        addon = bpy.context.preferences.addons.get(__name__)
        return addon.preferences if addon else None
    except Exception:
        return None


def _resource_folder(kind):
    prefs = _addon_preferences()
    if not prefs:
        return ""
    return prefs.grunge_source_folder if kind == "GRUNGE" else prefs.texture_source_folder



def get_materials():
    return [m for m in bpy.data.materials]


_IMAGE_EXTENSIONS={".png",".jpg",".jpeg",".tif",".tiff",".bmp",".exr",".hdr",".webp"}

def _image_files_recursive(folder):
    if not folder:
        return []
    folder=bpy.path.abspath(folder)
    if not os.path.isdir(folder):
        return []
    result=[]
    for root,dirs,files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() in _IMAGE_EXTENSIONS:
                result.append(os.path.join(root,name))
    return result

def _load_image_file(filepath):
    try:
        return bpy.data.images.load(filepath,check_existing=True)
    except Exception:
        return None


def _random_image_file(files, current_image=None):
    """Pick a different file when possible, avoiding the currently assigned image."""
    if not files:
        return None

    current_path = ""
    current_name = ""
    try:
        if current_image:
            current_path = bpy.path.abspath(current_image.filepath) if current_image.filepath else ""
            current_name = current_image.name
    except Exception:
        pass

    candidates=[]
    for filepath in files:
        if current_path and bpy.path.abspath(filepath) == current_path:
            continue
        candidates.append(filepath)

    # If there is only one available file, allow it to be selected again.
    if not candidates:
        candidates=list(files)

    return random.choice(candidates)


def ensure_material_node_setup(mat):
    # Mutating helper: use only from operators, never from draw().
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    output = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)

    if output is None:
        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (400, 0)

    if bsdf is None:
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (0, 0)

    surface_input = output.inputs.get("Surface")
    if surface_input and not any(
        l.to_node == output and l.to_socket == surface_input for l in links
    ):
        links.new(bsdf.outputs.get("BSDF"), surface_input)

    return bsdf


def get_existing_principled(mat):
    # Read-only helper safe for Blender UI draw callbacks.
    if not mat or not mat.node_tree:
        return None
    return next(
        (n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'),
        None
    )


# ------------------------------------------------------------------------
# Global material tiling + coordinate source
# ------------------------------------------------------------------------

_GLOBAL_TEXTURE_TYPES={"TEX_IMAGE","TEX_ENVIRONMENT","TEX_NOISE","TEX_VORONOI","TEX_WAVE","TEX_BRICK","TEX_MAGIC","TEX_MUSGRAVE","TEX_GRADIENT"}

def _find_global_mapping(mat):
    if not mat or not mat.node_tree:
        return None
    return next(
        (n for n in mat.node_tree.nodes
         if n.type=="MAPPING" and n.get("ML_GLOBAL_MAPPING")),
        None
    )

def _find_global_mapping_value(mat):
    if not mat or not mat.node_tree:
        return None
    return mat.node_tree.nodes.get("ML_Global_Tiling_Value")

def _is_effect_child(node):
    parent=node.parent
    while parent is not None:
        if parent.get("ML_EFFECT") or parent.get("ML_effect"):
            return True
        parent=parent.parent
    return False

def _base_texture_nodes(mat):
    if not mat or not mat.node_tree:
        return []
    return [
        n for n in mat.node_tree.nodes
        if n.type in _GLOBAL_TEXTURE_TYPES
        and not n.get("ML_GLOBAL_MAPPING")
        and not _is_effect_child(n)
        and not n.name.startswith("ML_")
    ]

def _capture_base_mapping_state(mat):
    if not mat or not mat.node_tree:
        return
    state=[]
    for tex in _base_texture_nodes(mat):
        vec=tex.inputs.get("Vector")
        if not vec:
            continue
        entry={
            "texture":tex.name,
            "texture_link_node":"",
            "texture_link_socket":"",
            "mapping_node":"",
            "mapping_link_node":"",
            "mapping_link_socket":"",
        }
        if vec.is_linked and vec.links:
            src=vec.links[0].from_socket
            entry["texture_link_node"]=src.node.name
            entry["texture_link_socket"]=src.name
            if src.node.type=="MAPPING":
                mapping_vec=src.node.inputs.get("Vector")
                if mapping_vec and mapping_vec.is_linked and mapping_vec.links:
                    msrc=mapping_vec.links[0].from_socket
                    entry["mapping_node"]=src.node.name
                    entry["mapping_link_node"]=msrc.node.name
                    entry["mapping_link_socket"]=msrc.name
        state.append(entry)
    mat["ML_BASE_MAPPING_STATE"]=json.dumps(state)
    mat["ML_BASE_MAPPING_CAPTURED"]=True

def _base_original_vector_socket(mat):
    """Return a representative original Vector source for the first base texture."""
    if not mat or not mat.node_tree:
        return None
    raw=mat.get("ML_BASE_MAPPING_STATE","")
    if not raw:
        return None
    try:
        state=json.loads(raw)
    except Exception:
        return None

    nodes=mat.node_tree.nodes
    for entry in state:
        if entry.get("mapping_link_node"):
            node=nodes.get(entry.get("mapping_link_node",""))
            socket=node.outputs.get(entry.get("mapping_link_socket","")) if node else None
            if socket:
                return socket

        node=nodes.get(entry.get("texture_link_node",""))
        socket=node.outputs.get(entry.get("texture_link_socket","")) if node else None
        if socket:
            return socket
    return None

def _restore_base_mapping_state(mat):
    if not mat or not mat.node_tree:
        return
    raw=mat.get("ML_BASE_MAPPING_STATE","")
    if not raw:
        return
    try:
        state=json.loads(raw)
    except Exception:
        return

    nodes=mat.node_tree.nodes
    links=mat.node_tree.links

    for entry in state:
        tex=nodes.get(entry.get("texture",""))
        if not tex:
            continue

        mapping_name=entry.get("mapping_node","")
        if mapping_name:
            mapping=nodes.get(mapping_name)
            if mapping:
                inp=mapping.inputs.get("Vector")
                if inp:
                    for link in list(inp.links):
                        links.remove(link)
                    src_node=nodes.get(entry.get("mapping_link_node",""))
                    src_socket=src_node.outputs.get(entry.get("mapping_link_socket","")) if src_node else None
                    if src_socket:
                        links.new(src_socket,inp)

        vec=tex.inputs.get("Vector")
        if not vec:
            continue
        for link in list(vec.links):
            links.remove(link)

        src_node=nodes.get(entry.get("texture_link_node",""))
        src_socket=src_node.outputs.get(entry.get("texture_link_socket","")) if src_node else None
        if src_socket:
            links.new(src_socket,vec)

def _remove_global_mapping_nodes(mat):
    if not mat or not mat.node_tree:
        return
    nodes=mat.node_tree.nodes
    for node in list(nodes):
        if node.get("ML_GLOBAL_MAPPING"):
            nodes.remove(node)

def _ensure_global_mapping(mat):
    if not mat or not mat.node_tree:
        return None

    nodes=mat.node_tree.nodes
    links=mat.node_tree.links

    master=_find_global_mapping(mat)
    if master is None:
        master=nodes.new("ShaderNodeMapping")
        master.name=_unique_node_name(mat,"ML_Global_Mapping")
        master.label="Base Material Mapping"
        master["ML_GLOBAL_MAPPING"]=True
        master.location=(-1600,900)

    coord=nodes.get("ML_Global_TexCoord")
    if coord is None:
        coord=nodes.new("ShaderNodeTexCoord")
        coord.name=_unique_node_name(mat,"ML_Global_TexCoord")
        coord.label="Base Material Coordinates"
        coord["ML_GLOBAL_MAPPING"]=True
        coord.location=(-1820,900)

    value=nodes.get("ML_Global_Tiling_Value")
    if value is None:
        value=nodes.new("ShaderNodeValue")
        value.name=_unique_node_name(mat,"ML_Global_Tiling_Value")
        value.label="Base Tiling"
        value["ML_GLOBAL_MAPPING"]=True
        value.location=(-1820,650)

    # The single Value controls all three Mapping Scale axes through the
    # vector input's implicit scalar-to-vector conversion.
    scale=master.inputs.get("Scale")
    if scale:
        for link in list(scale.links):
            if link.from_node != value:
                links.remove(link)
        if not scale.is_linked:
            links.new(value.outputs["Value"],scale)

    return master

def _connect_base_textures_to_global_mapping(mat,mapping_output):
    if not mat or not mat.node_tree or not mapping_output:
        return
    links=mat.node_tree.links

    for tex in _base_texture_nodes(mat):
        vec=tex.inputs.get("Vector")
        if not vec:
            continue
        for link in list(vec.links):
            links.remove(link)
        links.new(mapping_output,vec)

def _set_base_mapping_source(mat,source_name):
    if not mat or not mat.node_tree:
        return

    source_name=str(source_name)
    settings=mat.material_lab_settings
    tiling=float(settings.global_tiling)

    if source_name=="DEFAULT":
        # Default is the natural state. If Tiling is also back at 1, restore
        # the exact captured material and remove all Material Lab helper nodes.
        if abs(tiling-1.0) < 1e-6:
            _restore_base_mapping_state(mat)
            _remove_global_mapping_nodes(mat)
            mat["ML_BASE_MAPPING_MODE"]="DEFAULT"
            return

        # Tiling is being edited while Mapping remains Default. Preserve the
        # material's original coordinate source through our one Mapping node.
        if not mat.get("ML_BASE_MAPPING_CAPTURED",False):
            _capture_base_mapping_state(mat)

        master=_ensure_global_mapping(mat)
        original_source=_base_original_vector_socket(mat)
        if master and original_source:
            links=mat.node_tree.links
            target=master.inputs.get("Vector")
            if target:
                for link in list(target.links):
                    links.remove(link)
                links.new(original_source,target)
            _connect_base_textures_to_global_mapping(mat,master.outputs["Vector"])
            mat["ML_BASE_MAPPING_MODE"]="DEFAULT_TILING"
        return

    if not mat.get("ML_BASE_MAPPING_CAPTURED",False):
        _capture_base_mapping_state(mat)

    master=_ensure_global_mapping(mat)
    coord=mat.node_tree.nodes.get("ML_Global_TexCoord")
    if not master or not coord:
        return

    socket_name={
        "GENERATED":"Generated",
        "UV":"UV",
        "OBJECT":"Object"
    }.get(source_name,source_name)
    source=coord.outputs.get(socket_name)
    target=master.inputs.get("Vector")
    if not source or not target:
        return

    links=mat.node_tree.links
    for link in list(target.links):
        links.remove(link)
    links.new(source,target)

    _connect_base_textures_to_global_mapping(mat,master.outputs["Vector"])

    mat["ML_BASE_MAPPING_MODE"]=source_name

def _apply_material_global_tiling(mat,value):
    if not mat or not mat.node_tree:
        return

    master=_ensure_global_mapping(mat)
    if not master:
        return

    value_node=_find_global_mapping_value(mat)
    if value_node:
        value_node.outputs["Value"].default_value=float(value)

    mode=getattr(mat.material_lab_settings,"global_coordinate_source","DEFAULT")
    if mode=="DEFAULT":
        # Keep the original material coordinate source while applying the
        # one Material Lab Tiling controller.
        _set_base_mapping_source(mat,"DEFAULT")
    else:
        _set_base_mapping_source(mat,mode)

def _update_material_tiling(self,context):
    mat=getattr(self,"id_data",None)
    if mat and hasattr(mat,"node_tree"):
        _apply_material_global_tiling(mat,self.global_tiling)

def _update_global_coordinate_source(self,context):
    mat=getattr(self,"id_data",None)
    if mat and hasattr(mat,"node_tree"):
        _set_base_mapping_source(mat,self.global_coordinate_source)

def _update_ao_amount(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    ramp=_find_effect_node(mat,"AO","Ramp")
    if not ramp:
        return
    amount=max(0.01,min(1.0,float(self.ao_amount)))
    depth=max(0.0,min(1.0,float(self.ao_depth)))
    p0=max(0.0,min(depth,amount-0.01))
    p1=min(1.0,max(amount,p0+0.01))
    ramp.color_ramp.elements[0].position=p0
    ramp.color_ramp.elements[1].position=p1

def _update_ao_depth(self,context):
    _update_ao_amount(self,context)

def _update_ao_distance(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    ao=_find_effect_node(mat,"AO","AO","AMBIENT_OCCLUSION")
    if ao and ao.inputs.get("Distance"):
        ao.inputs["Distance"].default_value=float(self.ao_distance)


def _update_ao_color_blend(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    node=_find_effect_node(mat,"AO","ColorMix")
    if node and node.inputs.get("Fac"):
        node.inputs["Fac"].default_value=float(self.ao_color_blend)/10.0


def _update_dirt_amount(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return

    value=max(0.0,min(5.0,float(self.dirt_amount)))
    color_amount=_find_effect_node(mat,"Dirt","Amount")
    rough_amount=_find_effect_node(mat,"Dirt","AmountRoughness")
    color_mix=_find_effect_node(mat,"Dirt","Mix")
    color_blend=_find_effect_node(mat,"Dirt","ColorBlend")

    # Amount only changes the strength of the Dirt inside the existing mask.
    # It does not replace/rebuild the color chain.
    if color_amount and len(color_amount.inputs) > 1:
        color_amount.inputs[1].default_value=value
    if rough_amount and len(rough_amount.inputs) > 1:
        rough_amount.inputs[1].default_value=value

    if color_blend and color_mix and color_blend.outputs.get("Color"):
        links=mat.node_tree.links
        current=color_mix.inputs[2].links[0].from_socket if color_mix.inputs[2].is_linked else None
        if current != color_blend.outputs["Color"]:
            for link in list(color_mix.inputs[2].links):
                links.remove(link)
            links.new(color_blend.outputs["Color"],color_mix.inputs[2])


def _update_dirt_roughness(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    node=_find_effect_node(mat,"Dirt","RoughnessMix")
    if node and len(node.inputs) > 2:
        value=max(0.0,min(1.0,float(self.dirt_roughness)))
        node.inputs[2].default_value=(value,value,value,1.0)


def _update_dirt_noise_scale(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    node=_find_effect_node(mat,"Dirt","Texture","TEX_NOISE")
    if node and node.inputs.get("Scale"):
        node.inputs["Scale"].default_value=float(self.dirt_noise_scale)






def _update_grunge_alpha_image(self,context):
    if ML_GRUNGE_SYNCING:
        return
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    node=_find_effect_node(mat,"Grunge","AlphaImage","TEX_IMAGE")
    if node:
        node.image=self.grunge_alpha_image
    _update_grunge_alpha_source(self,context)


def _update_grunge_color_image(self,context):
    if ML_GRUNGE_SYNCING:
        return
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    node=_find_effect_node(mat,"Grunge","ColorImage","TEX_IMAGE")
    if node:
        node.image=self.grunge_color_image
    if self.grunge_color_image:
        self.grunge_color_source="TEXTURE"
    _update_grunge_color_source(self,context)





def _set_effect_mapping_source(mat,effect,source):
    if not mat or not mat.node_tree:
        return
    socket_name={"GENERATED":"Generated","UV":"UV","OBJECT":"Object"}.get(str(source),str(source))
    links=mat.node_tree.links
    if effect=="Grunge":
        pairs=(("ML_Grunge_AlphaCoord","AlphaMapping"),("ML_Grunge_ColorCoord","ColorMapping"))
    else:
        pairs=((f"ML_{effect}_Coord","Mapping"),)
    for coord_name,role in pairs:
        coord=mat.node_tree.nodes.get(coord_name)
        mapping=_find_effect_node(mat,effect,role)
        if not coord or not mapping or not mapping.inputs.get("Vector"):
            continue
        source_socket=coord.outputs.get(socket_name)
        if not source_socket:
            continue
        for link in list(mapping.inputs["Vector"].links):
            links.remove(link)
        links.new(source_socket,mapping.inputs["Vector"])

def _update_grunge_mapping(self,context):
    if ML_GRUNGE_SYNCING:
        return
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return

    # Legacy single mapping property keeps both chains synchronized.
    self.grunge_alpha_mapping_source=self.grunge_mapping_source
    self.grunge_color_mapping_source=self.grunge_mapping_source
    _update_grunge_alpha_mapping(self,context)
    _update_grunge_color_mapping(self,context)


def _update_grunge_alpha_mapping(self,context):
    if ML_GRUNGE_SYNCING:
        return
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    coord=mat.node_tree.nodes.get("ML_Grunge_AlphaCoord")
    mapping=_find_effect_node(mat,"Grunge","AlphaMapping")
    if not coord or not mapping:
        return
    socket_name={"GENERATED":"Generated","UV":"UV","OBJECT":"Object"}.get(str(self.grunge_alpha_mapping_source),str(self.grunge_alpha_mapping_source))
    source=coord.outputs.get(socket_name)
    if not source:
        return
    links=mat.node_tree.links
    for link in list(mapping.inputs["Vector"].links):
        links.remove(link)
    links.new(source,mapping.inputs["Vector"])

def _update_grunge_color_mapping(self,context):
    if ML_GRUNGE_SYNCING:
        return
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    mapping=_find_effect_node(mat,"Grunge","ColorMapping")
    if not mapping:
        return
    nodes=mat.node_tree.nodes
    links=mat.node_tree.links
    coord=nodes.get("ML_Grunge_ColorCoord")
    if coord is None:
        coord=nodes.new("ShaderNodeTexCoord")
        coord.name="ML_Grunge_ColorCoord"
        coord.label="Grunge Diffuse Coordinates"
        coord.parent=mapping.parent
        coord["ML_ROLE"]="ColorCoord"
    socket_name={"GENERATED":"Generated","UV":"UV","OBJECT":"Object"}.get(str(self.grunge_color_mapping_source),str(self.grunge_color_mapping_source))
    source=coord.outputs.get(socket_name)
    if source:
        for link in list(mapping.inputs["Vector"].links):
            links.remove(link)
        links.new(source,mapping.inputs["Vector"])
    image=_find_effect_node(mat,"Grunge","ColorImage","TEX_IMAGE")
    if image:
        for link in list(image.inputs["Vector"].links):
            links.remove(link)
        links.new(mapping.outputs["Vector"],image.inputs["Vector"])

def _update_grunge_alpha_source(self,context):
    if ML_GRUNGE_SYNCING:
        return
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    ramp=_find_effect_node(mat,"Grunge","AlphaRamp")
    noise=_find_effect_node(mat,"Grunge","AlphaNoise","TEX_NOISE")
    image=_find_effect_node(mat,"Grunge","AlphaImage","TEX_IMAGE")
    amount=_find_effect_node(mat,"Grunge","Amount")
    if not ramp or not amount:
        return

    links=mat.node_tree.links
    for link in list(ramp.inputs["Fac"].links):
        links.remove(link)

    # Preserve the existing two-handle Grunge Alpha ColorRamp.
    # Users now edit the black/white handles directly in the simplified UI.
    ramp.color_ramp.elements[0].color=(0.0,0.0,0.0,1.0)
    ramp.color_ramp.elements[-1].color=(1.0,1.0,1.0,1.0)

    if self.grunge_alpha_source=="TEXTURE" and image and image.image:
        links.new(image.outputs["Color"],ramp.inputs["Fac"])
        for l in list(amount.inputs[0].links):
            links.remove(l)
        links.new(ramp.outputs["Color"],amount.inputs[0])
        return

    if self.grunge_alpha_source=="NOISE" and noise:
        links.new(noise.outputs["Fac"],ramp.inputs["Fac"])
        for l in list(amount.inputs[0].links):
            links.remove(l)
        links.new(ramp.outputs["Color"],amount.inputs[0])
        return

    # None = no mask: Amount becomes a simple global visual amount.
    for l in list(amount.inputs[0].links):
        links.remove(l)
    amount.inputs[0].default_value=1.0

def _update_grunge_color_source(self,context):
    if ML_GRUNGE_SYNCING:
        return
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    mix=_find_effect_node(mat,"Grunge","ColorMix")
    color=_find_effect_node(mat,"Grunge","Color")
    image=_find_effect_node(mat,"Grunge","ColorImage","TEX_IMAGE")
    if not mix:
        return
    links=mat.node_tree.links
    for link in list(mix.inputs["Color2"].links):
        links.remove(link)
    if self.grunge_color_source=="TEXTURE" and self.grunge_color_image and image:
        image.image=self.grunge_color_image
        links.new(image.outputs["Color"],mix.inputs["Color2"])
    elif color:
        links.new(color.outputs["Color"],mix.inputs["Color2"])
    _update_grunge_color_mapping(self,context)

def _update_grunge_tiling(self,context):
    if ML_GRUNGE_SYNCING:
        return
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    for role,value in (("AlphaMapping",self.grunge_alpha_tiling),("ColorMapping",self.grunge_color_tiling)):
        node=_find_effect_node(mat,"Grunge",role)
        if node and node.inputs.get("Scale"):
            node.inputs["Scale"].default_value=(value,value,value)










def _update_hsv(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    hsv=_find_effect_node(mat,"HSV","HSV")
    if hsv:
        hsv.inputs["Hue"].default_value=float(self.hsv_hue)
        hsv.inputs["Saturation"].default_value=float(self.hsv_saturation)
        hsv.inputs["Value"].default_value=float(self.hsv_value)
        hsv.inputs["Fac"].default_value=float(self.hsv_amount)

def _update_dirt_ao_amount(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree: return
    ramp=_find_effect_node(mat,"Dirt","AORamp")
    if not ramp: return
    amount=max(0.01,min(1.0,float(self.dirt_ao_amount)))
    depth=max(0.0,min(1.0,float(self.dirt_ao_depth)))
    p0=max(0.0,min(depth,amount-0.01))
    p1=min(1.0,max(amount,p0+0.01))
    ramp.color_ramp.elements[0].position=p0
    ramp.color_ramp.elements[1].position=p1

def _update_dirt_ao_depth(self,context):
    _update_dirt_ao_amount(self,context)

def _update_dirt_ao_distance(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree: return
    ao=_find_effect_node(mat,"Dirt","AO","AMBIENT_OCCLUSION")
    if ao and ao.inputs.get("Distance"):
        ao.inputs["Distance"].default_value=float(self.dirt_ao_distance)

def _update_dirt_color_blend(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return

    blend=_find_effect_node(mat,"Dirt","ColorBlend")
    color=_find_effect_node(mat,"Dirt","Color")
    color_mix=_find_effect_node(mat,"Dirt","Mix")
    if not blend or not color_mix:
        return

    links=mat.node_tree.links

    if blend.inputs.get("Fac"):
        blend.inputs["Fac"].default_value=max(0.0,min(1.0,float(self.dirt_color_blend)))

    # A = the real/base color path, B = the Dirt color.
    # This makes Blending an actual, visible Base <-> Dirt control.
    base_input=color_mix.inputs[1] if len(color_mix.inputs) > 1 else None
    blend_base_input=blend.inputs[1] if len(blend.inputs) > 1 else None
    if base_input and blend_base_input:
        if base_input.is_linked:
            source=base_input.links[0].from_socket
            current=blend.inputs[1].links[0].from_socket if blend.inputs[1].is_linked else None
            if current != source:
                for link in list(blend.inputs[1].links):
                    links.remove(link)
                links.new(source,blend.inputs[1])
        else:
            if blend.inputs[1].is_linked:
                for link in list(blend.inputs[1].links):
                    links.remove(link)
            try:
                blend.inputs[1].default_value=base_input.default_value
            except Exception:
                pass

    if color and len(blend.inputs) > 2:
        current=blend.inputs[2].links[0].from_socket if blend.inputs[2].is_linked else None
        if current != color.outputs["Color"]:
            for link in list(blend.inputs[2].links):
                links.remove(link)
            links.new(color.outputs["Color"],blend.inputs[2])

    if blend.outputs.get("Color") and len(color_mix.inputs) > 2:
        current=color_mix.inputs[2].links[0].from_socket if color_mix.inputs[2].is_linked else None
        if current != blend.outputs["Color"]:
            for link in list(color_mix.inputs[2].links):
                links.remove(link)
            links.new(blend.outputs["Color"],color_mix.inputs[2])

def _update_dirt_color_source(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return

    color_mix=_find_effect_node(mat,"Dirt","Mix")
    color=_find_effect_node(mat,"Dirt","Color")
    image=_find_effect_node(mat,"Dirt","ColorImage","TEX_IMAGE")
    blend=_find_effect_node(mat,"Dirt","ColorBlend")
    if not color_mix or not blend:
        return

    links=mat.node_tree.links

    if len(blend.inputs) > 2:
        for l in list(blend.inputs[2].links):
            links.remove(l)

        if self.dirt_color_source=="TEXTURE" and self.dirt_color_image and image:
            links.new(image.outputs["Color"],blend.inputs[2])
        elif color:
            color.outputs["Color"].default_value=tuple(self.dirt_color)
            links.new(color.outputs["Color"],blend.inputs[2])

    if blend.outputs.get("Color") and len(color_mix.inputs) > 2:
        for l in list(color_mix.inputs[2].links):
            links.remove(l)
        links.new(blend.outputs["Color"],color_mix.inputs[2])

def _update_dirt_color_image(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree: return
    image=_find_effect_node(mat,"Dirt","ColorImage","TEX_IMAGE")
    mapping=_find_effect_node(mat,"Dirt","ColorMapping")
    if image:
        image.image=self.dirt_color_image
    if mapping and mapping.inputs.get("Scale"):
        t=float(self.dirt_color_tiling)
        mapping.inputs["Scale"].default_value=(t,t,t)
    _update_dirt_color_source(self,context)


def _update_dirt_mapping(self,context):
    mat=getattr(self,"id_data",None)
    if mat and mat.node_tree:
        _set_effect_mapping_source(mat,"Dirt",self.dirt_mapping_source)

def _update_dirt_color(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    node=_find_effect_node(mat,"Dirt","Color")
    if node:
        node.outputs["Color"].default_value=tuple(self.dirt_color)

def _update_grunge_noise(self,context):
    if ML_GRUNGE_SYNCING:
        return
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    node=_find_effect_node(mat,"Grunge","AlphaNoise","TEX_NOISE")
    if not node:
        return
    for name,value in (
        ("Scale",self.grunge_noise_scale),
        ("Detail",self.grunge_noise_detail),
        ("Roughness",self.grunge_noise_roughness),
        ("Lacunarity",self.grunge_noise_lacunarity),
        ("W",self.grunge_noise_seed),
        ("Distortion",self.grunge_noise_distortion),
    ):
        if node.inputs.get(name):
            node.inputs[name].default_value=float(value)
    try:
        node.normalize=bool(self.grunge_noise_normalize)
    except Exception:
        pass

def _update_grunge_amount(self,context):
    if ML_GRUNGE_SYNCING:
        return
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    node=mat.node_tree.nodes.get("ML_Grunge_Amount")
    if node is None:
        node=_find_effect_node(mat,"Grunge","Amount")
    if node and len(node.inputs)>1:
        node.inputs[1].default_value=max(0.0,min(1.0,float(self.grunge_amount)/5.0))


def _update_grunge_blending(self,context):
    if ML_GRUNGE_SYNCING:
        return
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    node=mat.node_tree.nodes.get("ML_Grunge_ColorMix")
    if node is None:
        node=_find_effect_node(mat,"Grunge","ColorMix")
    if node:
        node.inputs[0].default_value=max(0.0,min(1.0,float(self.grunge_blending)))



def _sync_grunge_ui_from_nodes(mat,settings):
    global ML_GRUNGE_SYNCING
    if not mat or not mat.node_tree or not settings:
        return
    nt=mat.node_tree
    ML_GRUNGE_SYNCING=True
    try:
        noise=nt.nodes.get("ML_Grunge_AlphaNoise")
        if noise:
            for prop_name,input_name in (
                ("grunge_noise_scale","Scale"),
                ("grunge_noise_detail","Detail"),
                ("grunge_noise_roughness","Roughness"),
                ("grunge_noise_lacunarity","Lacunarity"),
                ("grunge_noise_seed","W"),
                ("grunge_noise_distortion","Distortion"),
            ):
                inp=noise.inputs.get(input_name)
                if inp is not None:
                    setattr(settings,prop_name,float(inp.default_value))
            try:
                settings.grunge_noise_normalize=bool(noise.normalize)
            except Exception:
                pass
        amount=nt.nodes.get("ML_Grunge_Amount")
        if amount and len(amount.inputs)>1:
            settings.grunge_amount=max(0.0,min(5.0,float(amount.inputs[1].default_value)*5.0))
        blend=nt.nodes.get("ML_Grunge_ColorMix")
        if blend and blend.inputs.get("Fac"):
            settings.grunge_blending=max(0.0,min(1.0,float(blend.inputs["Fac"].default_value)))
    finally:
        ML_GRUNGE_SYNCING=False

def _update_grunge_mask_contrast(self,context):
    # Legacy compatibility hook. The Grunge UI now edits the actual ColorRamp
    # handles directly, so this property no longer drives the ramp.
    return

def _update_grunge_height(self,context):
    if ML_GRUNGE_SYNCING:
        return
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    bump=_find_effect_node(mat,"Grunge","Bump")
    ramp=_find_effect_node(mat,"Grunge","AlphaRamp")
    invert=_find_effect_node(mat,"Grunge","HeightInvert")
    if not bump:
        return

    h=float(self.grunge_height)
    if bump.inputs.get("Strength"):
        bump.inputs["Strength"].default_value=abs(h)

    if invert and ramp:
        invert.inputs[0].default_value=1.0
        for link in list(invert.inputs[1].links):
            mat.node_tree.links.remove(link)
        mat.node_tree.links.new(ramp.outputs["Color"],invert.inputs[1])

    if bump.inputs.get("Height"):
        for link in list(bump.inputs["Height"].links):
            mat.node_tree.links.remove(link)
        if h < 0.0 and invert:
            mat.node_tree.links.new(invert.outputs["Value"],bump.inputs["Height"])
        elif ramp:
            mat.node_tree.links.new(ramp.outputs["Color"],bump.inputs["Height"])

def _update_roughness_mapping(self,context):
    mat=getattr(self,"id_data",None)
    if mat and mat.node_tree:
        _set_effect_mapping_source(mat,"Roughness",self.roughness_mapping_source)

def _update_roughness(self,context):
    mat=getattr(self,"id_data",None)
    if not mat or not mat.node_tree:
        return
    noise=_find_effect_node(mat,"Roughness","Noise","TEX_NOISE")
    image=_find_effect_node(mat,"Roughness","Image","TEX_IMAGE")
    mapping=_find_effect_node(mat,"Roughness","Mapping")
    ramp=_find_effect_node(mat,"Roughness","Mask")
    amount=_find_effect_node(mat,"Roughness","Amount")
    if mapping and mapping.inputs.get("Scale"):
        v=float(self.roughness_tiling)
        mapping.inputs["Scale"].default_value=(v,v,v)
    if image:
        image.image=self.roughness_image
    if noise:
        for name,value in (
            ("Scale",self.roughness_noise_scale),
            ("Detail",self.roughness_detail),
            ("Roughness",self.roughness_noise_roughness),
            ("Lacunarity",self.roughness_lacunarity),
            ("W",self.roughness_seed),
            ("Distortion",self.roughness_distortion),
        ):
            if noise.inputs.get(name):
                noise.inputs[name].default_value=float(value)
    if ramp:
        links=mat.node_tree.links
        for link in list(ramp.inputs["Fac"].links): links.remove(link)
        if self.roughness_source=="IMAGE" and image and image.image:
            links.new(image.outputs["Color"],ramp.inputs["Fac"])
        elif self.roughness_source=="NOISE" and noise:
            links.new(noise.outputs["Fac"],ramp.inputs["Fac"])
    if amount:
        amount.inputs[1].default_value=float(self.roughness_value)



# ------------------------------------------------------------------------
# Properties
# ------------------------------------------------------------------------
# ------------------------------------------------------------------------
# Properties
# ------------------------------------------------------------------------

def _poll_mixer_material(self, material):
    if not material or getattr(material, "users", 0) < 1:
        return False
    obj=bpy.context.active_object
    if obj and getattr(obj, "active_material", None) == material:
        return False
    return True


def _poll_mixer_empty(self, obj):
    return bool(obj and obj.type in {'EMPTY','CURVE'})


def _mixer_layer_index(mat, layer):
    try:
        return list(mat.material_lab_settings.mixer_layers).index(layer)
    except Exception:
        return -1





def _view_mask_clear_state(mat):
    if not mat:
        return
    for key in (
        "ML_VIEW_MASK_ACTIVE",
        "ML_VIEW_MASK_EFFECT",
        "ML_VIEW_MASK_INDEX",
        "ML_VIEW_MASK_NODE",
        "ML_VIEW_MASK_SOCKET",
        "ML_VIEW_MASK_OUTPUT_NODE",
        "ML_VIEW_MASK_OUTPUT_SOCKET",
        "ML_VIEW_MASK_PREVIEW_NODE",
        "ML_VIEW_MASK_PREVIEW_TYPE",
    ):
        try:
            del mat[key]
        except Exception:
            pass


def _view_mask_restore_output(mat):
    if not mat or not mat.node_tree:
        return
    nodes=mat.node_tree.nodes
    links=mat.node_tree.links
    output=nodes.get(str(mat.get("ML_VIEW_MASK_OUTPUT_NODE","")))
    if not output:
        output=next((n for n in nodes if n.type=="OUTPUT_MATERIAL"),None)
    surface=output.inputs.get("Surface") if output else None
    if not surface:
        _view_mask_clear_state(mat)
        return

    for link in list(surface.links):
        links.remove(link)

    original_node=nodes.get(str(mat.get("ML_VIEW_MASK_OUTPUT_NODE_SOURCE","")))
    original_socket_name=str(mat.get("ML_VIEW_MASK_OUTPUT_SOCKET_SOURCE",""))
    if original_node and original_socket_name:
        source=original_node.outputs.get(original_socket_name)
        if source:
            try:
                links.new(source,surface)
            except Exception:
                pass

    preview=nodes.get(str(mat.get("ML_VIEW_MASK_PREVIEW_NODE","")))
    if preview:
        try:
            nodes.remove(preview)
        except Exception:
            pass

    _view_mask_clear_state(mat)


def _view_mask_show(mat,mask_socket,effect,index):
    if not mat or not mat.node_tree or not mask_socket:
        return False

    nodes=mat.node_tree.nodes
    links=mat.node_tree.links
    output=next((n for n in nodes if n.type=="OUTPUT_MATERIAL" and n.is_active_output),None)
    if not output:
        output=next((n for n in nodes if n.type=="OUTPUT_MATERIAL"),None)
    if not output:
        return False

    surface=output.inputs.get("Surface")
    if not surface:
        return False

    if mat.get("ML_VIEW_MASK_ACTIVE"):
        _view_mask_restore_output(mat)

    mat["ML_VIEW_MASK_ACTIVE"]=True
    mat["ML_VIEW_MASK_EFFECT"]=effect
    mat["ML_VIEW_MASK_INDEX"]=int(index)
    mat["ML_VIEW_MASK_NODE"]=mask_socket.node.name
    mat["ML_VIEW_MASK_SOCKET"]=mask_socket.name
    mat["ML_VIEW_MASK_OUTPUT_NODE"]=output.name

    # Save the exact shader currently feeding Surface.
    if surface.is_linked:
        original=surface.links[0].from_socket
        mat["ML_VIEW_MASK_OUTPUT_NODE_SOURCE"]=original.node.name
        mat["ML_VIEW_MASK_OUTPUT_SOCKET_SOURCE"]=original.name
    else:
        mat["ML_VIEW_MASK_OUTPUT_NODE_SOURCE"]=""
        mat["ML_VIEW_MASK_OUTPUT_SOCKET_SOURCE"]=""

    # Build a temporary unlit preview. This guarantees the viewport shows only
    # the mask, not the Base Color/BSDF/material layers underneath.
    preview=nodes.new("ShaderNodeEmission")
    preview.name=_unique_node_name(mat,"ML_ViewMask_Preview")
    preview.label=f"View Mask • {effect}"
    preview.location=(output.location.x-260.0,output.location.y)
    preview["ML_VIEW_MASK_PREVIEW"]=True
    preview["ML_VIEW_MASK_PREVIEW_EFFECT"]=effect

    links.new(mask_socket,preview.inputs["Color"])
    if preview.inputs.get("Strength"):
        preview.inputs["Strength"].default_value=1.0
    links.new(preview.outputs["Emission"],surface)

    mat["ML_VIEW_MASK_PREVIEW_NODE"]=preview.name
    mat["ML_VIEW_MASK_PREVIEW_TYPE"]="EMISSION"

    return True



def _mixer_mask_socket(mat,idx):
    if not mat or not mat.node_tree or idx<0 or idx>=len(mat.material_lab_settings.mixer_layers):
        return None
    layer=mat.material_lab_settings.mixer_layers[idx]
    if layer.mask_source=="NONE":
        return None
    nodes=mat.node_tree.nodes
    if layer.mask_source in {"NOISE","TEXTURE"}:
        ramp=next((n for n in nodes if n.get("ML_MIXER_MASK_RAMP_INDEX")==idx),None)
        return ramp.outputs.get("Color") if ramp else None
    if layer.mask_source=="BLEED":
        node=next((n for n in nodes if n.get("ML_MIXER_BLEED_RAMP_INDEX")==idx),None)
        return node.outputs.get("Color") if node else None
    return None


def _mixer_preserve_positions(mat,idx):
    if not mat or not mat.node_tree:
        return {}
    frame=_find_effect_frame(mat,"Material Mixer")
    if not frame:
        return {}
    source_frame=next(
        (n for n in mat.node_tree.nodes
         if n.parent==frame and n.type=="FRAME"
         and n.get("ML_MIXER_SOURCE_INDEX")==idx),
        None
    )
    targets=[]
    if source_frame:
        targets.append(source_frame)
        targets.extend(n for n in mat.node_tree.nodes if n.parent==source_frame)
    else:
        targets.extend(
            n for n in mat.node_tree.nodes
            if n.parent==frame and n.get("ML_MIXER_GENERATED")
        )
    return {n.name:(float(n.location.x),float(n.location.y)) for n in targets}


def _mixer_restore_positions(mat,saved):
    if not mat or not mat.node_tree:
        return
    for name,(x,y) in saved.items():
        node=mat.node_tree.nodes.get(name)
        if node:
            node.location.x=x
            node.location.y=y


class ML_OT_view_mask(Operator):
    bl_idname="material_lab.view_mask"
    bl_label="View Mask"
    bl_options={'REGISTER','UNDO'}

    effect: StringProperty(default="")
    index: IntProperty(default=-1)

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat or not mat.node_tree:
            return {'CANCELLED'}

        active_effect=str(mat.get("ML_VIEW_MASK_EFFECT",""))
        active_index=int(mat.get("ML_VIEW_MASK_INDEX",-1))
        active=bool(mat.get("ML_VIEW_MASK_ACTIVE",False))
        if active and active_effect==self.effect and active_index==self.index:
            _view_mask_restore_output(mat)
            return {'FINISHED'}

        socket=None
        if self.effect=="Grunge":
            node=_find_effect_node(mat,"Grunge","AlphaRamp")
            socket=node.outputs.get("Color") if node else None
        elif self.effect=="Dirt":
            node=_find_effect_node(mat,"Dirt","MaskRamp")
            socket=node.outputs.get("Color") if node else None
        elif self.effect=="Roughness":
            node=_find_effect_node(mat,"Roughness","Mask")
            socket=node.outputs.get("Color") if node else None
        elif self.effect=="AO":
            node=_find_effect_node(mat,"AO","Ramp")
            socket=node.outputs.get("Color") if node else None
        elif self.effect=="Material Mixer":
            socket=_mixer_mask_socket(mat,self.index)

        if not socket or not _view_mask_show(mat,socket,self.effect,self.index):
            return {'CANCELLED'}

        st=mat.material_lab_settings
        st.grunge_view_mask=False
        st.dirt_view_mask=False
        st.roughness_view_mask=False
        st.ao_view_mask=False
        for layer in st.mixer_layers:
            layer.view_mask=False

        if self.effect=="Grunge":
            st.grunge_view_mask=True
        elif self.effect=="Dirt":
            st.dirt_view_mask=True
        elif self.effect=="Roughness":
            st.roughness_view_mask=True
        elif self.effect=="AO":
            st.ao_view_mask=True
        elif self.effect=="Material Mixer" and 0 <= self.index < len(st.mixer_layers):
            st.mixer_layers[self.index].view_mask=True

        return {'FINISHED'}


def _mixer_has_legacy_mask_blur(mat, idx=None):
    """Detect the obsolete node-based mask blur left by older ShaderPit versions."""
    if not mat or not mat.node_tree:
        return False
    nodes=mat.node_tree.nodes
    legacy_keys=(
        "ML_MIXER_MASK_BLUR_IMAGE_INDEX",
        "ML_MIXER_MASK_BLUR_OFFSET_INDEX",
        "ML_MIXER_MASK_BLUR_FINAL_INDEX",
        "ML_MIXER_MASK_BLUR_VALUE_INDEX",
        "ML_MIXER_MASK_BLUR_AVG_INDEX",
    )
    for n in nodes:
        if any(key in n for key in legacy_keys):
            if idx is None or any(int(n.get(key,-999))==idx for key in legacy_keys if key in n):
                return True
    return False


def _update_mixer_layer_nodes_in_place(mat, idx):
    if not mat or not mat.node_tree or idx < 0:
        return
    if _mixer_has_legacy_mask_blur(mat,idx):
        _rebuild_material_mixer(mat)
        return
    settings=mat.material_lab_settings
    if idx >= len(settings.mixer_layers):
        return
    layer=settings.mixer_layers[idx]
    nodes=mat.node_tree.nodes
    links=mat.node_tree.links

    factor=next((n for n in nodes if n.get("ML_MIXER_FACTOR_INDEX")==idx),None)
    mix=next((n for n in nodes if n.get("ML_MIXER_MIX_INDEX")==idx),None)
    if factor and len(factor.inputs)>1:
        if not layer.enabled:
            factor.inputs[1].default_value=0.0
        elif layer.blend_mode=="REPLACE":
            factor.inputs[1].default_value=1.0
        else:
            factor.inputs[1].default_value=float(layer.amount)

    if mix:
        if not factor:
            if not layer.enabled:
                mix.inputs[0].default_value=0.0
            elif layer.blend_mode=="REPLACE":
                mix.inputs[0].default_value=1.0
            else:
                mix.inputs[0].default_value=float(layer.amount)
        elif not layer.enabled:
            mix.inputs[0].default_value=0.0

        mode_label="Replace" if layer.blend_mode=="REPLACE" else f"{layer.amount*100:.0f}%"
        mix.label=f"Material {idx+2} • {mode_label}"

    # Existing Noise/Texture mask path stays unchanged.
    if layer.mask_source in {"NOISE","TEXTURE"}:
        mapping=next((n for n in nodes if n.get("ML_MIXER_MASK_MAPPING_INDEX")==idx),None)
        coord=next((n for n in nodes if n.get("ML_MIXER_MASK_COORD_INDEX")==idx),None)
        if coord:
            try:
                coord.object=layer.mask_object if layer.mask_object and layer.mask_object.type=="EMPTY" else None
            except Exception:
                pass
        if mapping:
            mapping.inputs["Scale"].default_value=(float(layer.mask_tiling),)*3
        if coord and mapping:
            source=coord.outputs.get(str(layer.mask_mapping_source))
            if source:
                for l in list(mapping.inputs["Vector"].links):
                    links.remove(l)
                links.new(source,mapping.inputs["Vector"])

        noise=next((n for n in nodes if n.get("ML_MIXER_MASK_NOISE_INDEX")==idx),None)
        image=next((n for n in nodes if n.get("ML_MIXER_MASK_IMAGE_INDEX")==idx),None)
        ramp=next((n for n in nodes if n.get("ML_MIXER_MASK_RAMP_INDEX")==idx),None)

        if noise:
            for name,value in (
                ("Scale",layer.mask_noise_scale),
                ("Detail",layer.mask_noise_detail),
                ("Roughness",layer.mask_noise_roughness),
                ("Lacunarity",layer.mask_noise_lacunarity),
                ("W",layer.mask_noise_seed),
                ("Distortion",layer.mask_noise_distortion),
            ):
                if noise.inputs.get(name):
                    noise.inputs[name].default_value=float(value)
            try:
                noise.normalize=bool(layer.mask_noise_normalize)
            except Exception:
                pass
            if mapping and noise.inputs.get("Vector") and not noise.inputs["Vector"].is_linked:
                links.new(mapping.outputs["Vector"],noise.inputs["Vector"])

        if image:
            image.image=layer.mask_image
            if mapping and image.inputs.get("Vector") and not image.inputs["Vector"].is_linked:
                links.new(mapping.outputs["Vector"],image.inputs["Vector"])

        if ramp:
            # Keep the real ColorRamp editable. If the legacy slider is touched,
            # it still compresses the handles, but UI no longer exposes the slider.
            if abs(float(layer.mask_contrast)-0.08)>1e-6 and layer.mask_ramp_black==0.0 and layer.mask_ramp_white==1.0:
                c=max(0.0,min(1.0,float(layer.mask_contrast)))
                _set_mixer_ramp(ramp.color_ramp,0.5-0.49*c,0.5+0.49*c)

        if factor and ramp and ramp.outputs.get("Color") and not factor.inputs[0].is_linked:
            links.new(ramp.outputs["Color"],factor.inputs[0])

    elif layer.mask_source=="BLEED":
        coord=next((n for n in nodes if n.get("ML_MIXER_BLEED_COORD_INDEX")==idx),None)
        grad_mapping=next((n for n in nodes if n.get("ML_MIXER_BLEED_MAPPING_INDEX")==idx),None)
        gradient=next((n for n in nodes if n.get("ML_MIXER_BLEED_GRADIENT_INDEX")==idx),None)
        bleed_ramp=next((n for n in nodes if n.get("ML_MIXER_BLEED_RAMP_INDEX")==idx),None)
        noise_mapping=next((n for n in nodes if n.get("ML_MIXER_BLEED_NOISE_MAPPING_INDEX")==idx),None)
        bleed_noise=next((n for n in nodes if n.get("ML_MIXER_BLEED_NOISE_INDEX")==idx),None)
        noise_ramp=next((n for n in nodes if n.get("ML_MIXER_BLEED_NOISE_RAMP_INDEX")==idx),None)
        break_mult=next((n for n in nodes if n.get("ML_MIXER_BLEED_BREAK_MULTIPLY_INDEX")==idx),None)
        break_sub=next((n for n in nodes if n.get("ML_MIXER_BLEED_BREAK_SUBTRACT_INDEX")==idx),None)

        if not (coord and grad_mapping and gradient and bleed_ramp and noise_mapping and bleed_noise and noise_ramp and break_mult and break_sub):
            _rebuild_material_mixer(mat)
            return

        try:
            coord.object=layer.bleed_object if layer.bleed_object else None
        except Exception:
            pass

        grad_mapping.inputs["Scale"].default_value=(1.0,1.0,1.0)
        grad_mapping.inputs["Rotation"].default_value[1]=-1.5707963267948966
        noise_mapping.inputs["Scale"].default_value=(float(layer.bleed_tiling),)*3

        if coord.outputs.get("Object"):
            for target_node in (grad_mapping,noise_mapping):
                vec=target_node.inputs["Vector"]
                if not vec.is_linked or vec.links[0].from_node != coord:
                    for link in list(vec.links):
                        links.remove(link)
                    links.new(coord.outputs["Object"],vec)

        if not gradient.inputs["Vector"].is_linked:
            links.new(grad_mapping.outputs["Vector"],gradient.inputs["Vector"])
        if not bleed_noise.inputs["Vector"].is_linked:
            links.new(noise_mapping.outputs["Vector"],bleed_noise.inputs["Vector"])

        for name,value in (
            ("Scale",layer.bleed_noise_scale),
            ("Detail",layer.bleed_noise_detail),
            ("Roughness",layer.bleed_noise_roughness),
            ("Lacunarity",layer.bleed_noise_lacunarity),
            ("W",layer.bleed_noise_seed),
            ("Distortion",layer.bleed_noise_distortion),
        ):
            if bleed_noise.inputs.get(name):
                bleed_noise.inputs[name].default_value=float(value)
        try:
            bleed_noise.normalize=bool(layer.bleed_noise_normalize)
        except Exception:
            pass

        break_mult.inputs[1].default_value=float(layer.bleed_break_border)

        if not break_mult.inputs[0].is_linked:
            links.new(noise_ramp.outputs["Color"],break_mult.inputs[0])

        if not break_sub.inputs[0].is_linked:
            links.new(gradient.outputs["Fac"],break_sub.inputs[0])
        if not break_sub.inputs[1].is_linked:
            links.new(break_mult.outputs["Value"],break_sub.inputs[1])
        if not bleed_ramp.inputs["Fac"].is_linked:
            links.new(break_sub.outputs["Value"],bleed_ramp.inputs["Fac"])
        if factor and not factor.inputs[0].is_linked:
            links.new(bleed_ramp.outputs["Color"],factor.inputs[0])





def _update_mixer_layer_height(self, context):
    obj=getattr(context,"active_object",None)
    mat=obj.active_material if obj and obj.type=="MESH" else None
    idx=_mixer_layer_index(mat,self) if mat else -1
    if idx < 0 or not mat or not mat.node_tree:
        return

    # Height is an in-place control. It never rebuilds or relayouts Material 2.
    saved=_mixer_preserve_positions(mat,idx)
    nodes=mat.node_tree.nodes
    links=mat.node_tree.links
    bump=next((n for n in nodes if n.get("ML_MIXER_HEIGHT_BUMP_INDEX")==idx),None)
    invert=next((n for n in nodes if n.get("ML_MIXER_HEIGHT_INVERT_INDEX")==idx),None)
    factor=next((n for n in nodes if n.get("ML_MIXER_FACTOR_INDEX")==idx),None)

    if not bump or not factor:
        _mixer_restore_positions(mat,saved)
        return

    h=float(self.height)
    if bump.inputs.get("Strength"):
        bump.inputs["Strength"].default_value=abs(h)

    if invert:
        invert.inputs[0].default_value=1.0

    if bump.inputs.get("Height"):
        desired=invert.outputs["Value"] if h<0.0 and invert else factor.outputs["Value"]
        current=bump.inputs["Height"].links[0].from_socket if bump.inputs["Height"].is_linked else None
        if current != desired:
            for link in list(bump.inputs["Height"].links):
                links.remove(link)
            links.new(desired,bump.inputs["Height"])

    _mixer_restore_positions(mat,saved)


def _update_mixer_layer_amount(self, context):
    obj=getattr(context,"active_object",None)
    mat=obj.active_material if obj and obj.type=="MESH" else None
    idx=_mixer_layer_index(mat,self) if mat else -1
    if idx>=0:
        _update_mixer_layer_nodes_in_place(mat,idx)


def _update_mixer_layer_mask(self, context):
    try:
        obj=context.active_object
        mat=obj.active_material if obj and obj.type=="MESH" else None
        if mat:
            idx=_mixer_layer_index(mat,self)
            if idx >= 0:
                settings=mat.material_lab_settings
                layer=settings.mixer_layers[idx]
                if (
                    obj.mode=="TEXTURE_PAINT"
                    and settings.mixer_paint_layer==idx
                    and layer.mask_source=="TEXTURE"
                    and layer.mask_image
                ):
                    _mixer_set_paint_canvas(context,layer.mask_image)
    except Exception:
        pass

    obj=getattr(context,"active_object",None)
    mat=obj.active_material if obj and obj.type=="MESH" else None
    idx=_mixer_layer_index(mat,self) if mat else -1
    if idx < 0 or not mat:
        return

    nodes=mat.node_tree.nodes
    layer=mat.material_lab_settings.mixer_layers[idx]
    if layer.mask_source=="BLEED":
        complete=(
            any(n.get("ML_MIXER_BLEED_BREAK_MULTIPLY_INDEX")==idx for n in nodes),
            any(n.get("ML_MIXER_BLEED_BREAK_SUBTRACT_INDEX")==idx for n in nodes),
        )
        if not all(complete):
            _rebuild_material_mixer(mat)
            return
    elif layer.mask_source in {"NOISE","TEXTURE"}:
        if not any(n.get("ML_MIXER_MASK_RAMP_INDEX")==idx for n in nodes):
            _rebuild_material_mixer(mat)
            return

    _update_mixer_layer_nodes_in_place(mat,idx)




def _update_mixer_layer_material(self, context):
    obj = getattr(context, "active_object", None)
    mat = obj.active_material if obj and obj.type == "MESH" else None
    if mat and _effect_nodes(mat, "Material Mixer"):
        try:
            _rebuild_material_mixer(mat)
        except Exception:
            pass

def _update_mixer_live_paint_value(self, context):
    """Drive the actual Image Paint brush Value as a black-to-white slider."""
    try:
        v=max(0.0,min(1.0,float(self.live_paint_value)))
        scene=getattr(context,"scene",None)
        tool_settings=getattr(scene,"tool_settings",None) if scene else None
        if tool_settings is None:
            return

        # Match Blender's native color-picker Value behavior: the brush color
        # remains neutral gray, so Value is exactly the black-to-white amount.
        paint=getattr(tool_settings,"image_paint",None)
        brush=getattr(paint,"brush",None) if paint else None
        if brush is not None and hasattr(brush,"color"):
            brush.color=(v,v,v)

        # Unified Color can override Brush.color in Texture Paint. Set it too
        # so the slider controls the same color regardless of that setting.
        # Blender 5.2 stores Unified Paint Settings on the mode-specific Paint
        # struct, not directly on ToolSettings.
        unified=getattr(paint,"unified_paint_settings",None) if paint else None
        if unified is not None and hasattr(unified,"color"):
            try:
                unified.use_unified_color=True
            except Exception:
                pass
            unified.color=(v,v,v)
    except Exception:
        pass


class ML_MixerLayer(PropertyGroup):
    material: PointerProperty(
        name="Material",
        type=bpy.types.Material,
        poll=_poll_mixer_material,
        update=_update_mixer_layer_material
    )
    blend_mode: EnumProperty(
        name="Mode",
        items=(
            ("MIX","Mix","Blend the base and added material using the current Blend amount"),
            ("REPLACE","Replace","Use the mask to replace the base with the added material"),
        ),
        default="MIX",
        update=_update_mixer_layer_amount
    )
    amount: FloatProperty(
        name="Blend",
        default=0.5,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_update_mixer_layer_amount
    )
    height: FloatProperty(
        name="Height",
        default=0.0,
        min=-1.0,
        max=1.0,
        soft_min=-1.0,
        soft_max=1.0,
        update=_update_mixer_layer_height
    )
    enabled: BoolProperty(
        name="Enabled",
        default=True,
        update=_update_mixer_layer_amount
    )
    mask_source: EnumProperty(
        name="Mask",
        items=(
            ("NONE","None","No mask"),
            ("NOISE","Noise","Procedural mask"),
            ("TEXTURE","Texture","Image mask"),
            ("BLEED","Bleed","Material bleed controlled by an Empty"),
        ),
        default="NONE",
        update=_update_mixer_layer_material
    )
    mask_mapping_source: EnumProperty(
        name="Coordinate",
        items=(
            ("UV","UV","UV"),
            ("Generated","Generated","Generated"),
            ("Object","Object","Object"),
        ),
        default="Generated",
        update=_update_mixer_layer_mask
    )
    mask_image: PointerProperty(
        name="Mask Texture",
        type=bpy.types.Image,
        update=_update_mixer_layer_mask
    )
    live_paint_value: FloatProperty(
        name="Brush Value", default=1.0, min=0.0, max=1.0,
        soft_min=0.0, soft_max=1.0, subtype='FACTOR',
        update=_update_mixer_live_paint_value
    )
    mask_tiling: FloatProperty(
        name="Tiling", default=1.0, min=0.0, max=100.0, soft_min=0.0, soft_max=10.0,
        update=_update_mixer_layer_mask
    )
    mask_object: PointerProperty(
        name="Object",
        type=bpy.types.Object,
        poll=_poll_mixer_empty,
        update=_update_mixer_layer_mask
    )
    mask_noise_scale: FloatProperty(
        name="Scale", default=1.3, min=0.0, max=100.0, soft_min=0.0, soft_max=20.0,
        update=_update_mixer_layer_mask
    )
    mask_noise_detail: FloatProperty(
        name="Detail", default=15.0, min=0.0, max=100.0, soft_min=0.0, soft_max=20.0,
        update=_update_mixer_layer_mask
    )
    mask_noise_roughness: FloatProperty(
        name="Roughness", default=0.6, min=0.0, max=1.0, soft_min=0.0, soft_max=1.0,
        update=_update_mixer_layer_mask
    )
    mask_noise_lacunarity: FloatProperty(
        name="Lacunarity", default=2.0, min=0.0, max=100.0, soft_min=0.0, soft_max=10.0,
        update=_update_mixer_layer_mask
    )
    mask_noise_seed: FloatProperty(
        name="Seed", default=1.0, min=0.0, max=10.0, soft_min=0.0, soft_max=10.0,
        update=_update_mixer_layer_mask
    )
    mask_noise_distortion: FloatProperty(
        name="Distortion", default=0.0, min=0.0, max=100.0, soft_min=0.0, soft_max=20.0,
        update=_update_mixer_layer_mask
    )
    mask_noise_normalize: BoolProperty(name="Normalize",default=False,update=_update_mixer_layer_mask)
    # Legacy compatibility property. The UI now uses the real ColorRamp node.
    mask_contrast: FloatProperty(name="Contrast",default=0.08,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_mixer_layer_mask)
    mask_ramp_black: FloatProperty(name="Mask Black",default=0.0,min=0.0,max=1.0,update=None)
    mask_ramp_white: FloatProperty(name="Mask White",default=1.0,min=0.0,max=1.0,update=None)
    mask_ramp_collapsed: BoolProperty(default=True)
    view_mask: BoolProperty(default=False)

    bleed_object: PointerProperty(
        name="Object",
        type=bpy.types.Object,
        poll=_poll_mixer_empty,
        update=_update_mixer_layer_mask
    )
    bleed_break_border: FloatProperty(
        name="Break Border",
        default=0.0,
        min=0.0,
        max=1.0,
        soft_min=0.0,
        soft_max=1.0,
        subtype='FACTOR',
        update=_update_mixer_layer_mask
    )
    bleed_tiling: FloatProperty(
        name="Noise Tiling", default=1.0, min=0.0, max=100.0, soft_min=0.0, soft_max=10.0,
        update=_update_mixer_layer_mask
    )
    bleed_noise_scale: FloatProperty(
        name="Scale", default=4.0, min=0.0, max=100.0, soft_min=0.0, soft_max=20.0,
        update=_update_mixer_layer_mask
    )
    bleed_noise_detail: FloatProperty(
        name="Detail", default=8.0, min=0.0, max=100.0, soft_min=0.0, soft_max=20.0,
        update=_update_mixer_layer_mask
    )
    bleed_noise_roughness: FloatProperty(
        name="Roughness", default=0.7, min=0.0, max=1.0, soft_min=0.0, soft_max=1.0,
        update=_update_mixer_layer_mask
    )
    bleed_noise_lacunarity: FloatProperty(
        name="Lacunarity", default=2.0, min=0.0, max=100.0, soft_min=0.0, soft_max=10.0,
        update=_update_mixer_layer_mask
    )
    bleed_noise_seed: FloatProperty(
        name="Seed", default=1.0, min=0.0, max=10.0, soft_min=0.0, soft_max=10.0,
        update=_update_mixer_layer_mask
    )
    bleed_noise_distortion: FloatProperty(
        name="Distortion", default=0.35, min=0.0, max=100.0, soft_min=0.0, soft_max=20.0,
        update=_update_mixer_layer_mask
    )
    bleed_noise_normalize: BoolProperty(name="Normalize",default=False,update=_update_mixer_layer_mask)
    bleed_noise_collapsed: BoolProperty(default=False)
    bleed_ramp_black: FloatProperty(name="Bleed Black",default=0.0,min=0.0,max=1.0,update=None)
    bleed_ramp_white: FloatProperty(name="Bleed White",default=1.0,min=0.0,max=1.0,update=None)
    bleed_noise_ramp_black: FloatProperty(name="Bleed Noise Black",default=0.0,min=0.0,max=1.0,update=None)
    bleed_noise_ramp_white: FloatProperty(name="Bleed Noise White",default=1.0,min=0.0,max=1.0,update=None)
    bleed_ramp_collapsed: BoolProperty(default=True)
    bleed_noise_ramp_collapsed: BoolProperty(default=True)
    mask_noise_collapsed: BoolProperty(default=False)


class ML_MaterialSettings(PropertyGroup):
    mixer_prev_canvas_source: StringProperty(
        name="Previous Paint Canvas Source",
        default="MATERIAL",
        options={'HIDDEN'}
    )
    mixer_prev_canvas_image: PointerProperty(
        name="Previous Paint Canvas Image",
        type=bpy.types.Image,
        options={'HIDDEN'}
    )
    mixer_paint_layer: IntProperty(
        name="Painting Layer",
        default=-1,
        options={'HIDDEN'}
    )
    shaderpit_tab: EnumProperty(
        name="ShaderPit Section",
        items=(
            ("EFFECTS", "Effects", "ShaderPit surface effects"),
            ("MIXER", "Material Mixer", "ShaderPit material mixing tool"),
        ),
        default="EFFECTS",
    )
    mixer_layers: CollectionProperty(type=ML_MixerLayer)
    mixer_active_index: IntProperty(default=0, min=0, max=5)
    mixer_collapsed: BoolProperty(default=False)

    global_tiling: FloatProperty(name="Tiling",default=1.0,min=-100.0,max=100.0,soft_min=-10.0,soft_max=10.0,update=_update_material_tiling)
    global_coordinate_source: EnumProperty(
        name="Mapping",
        items=(
            ("DEFAULT","Default","Leave the base material mapping unchanged"),
            ("GENERATED","Generated","Connect base material texture nodes to Generated"),
            ("UV","UV","Connect base material texture nodes to UV"),
            ("OBJECT","Object","Connect base material texture nodes to Object"),
        ),
        default="DEFAULT",
        update=_update_global_coordinate_source
    )

    ao_amount: FloatProperty(name="AO Amount",default=0.5,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_ao_amount)
    ao_depth: FloatProperty(name="AO Depth",default=0.05,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_ao_depth)
    ao_distance: FloatProperty(name="AO Distance",default=0.5,min=0.0,max=2.0,soft_min=0.0,soft_max=2.0,update=_update_ao_distance)
    ao_color_blend: FloatProperty(
        name="AO Color Blend", default=10.0,
        min=0.0, max=100.0, soft_min=0.0, soft_max=10.0,
        update=_update_ao_color_blend
    )

    dirt_ao_amount: FloatProperty(name="Amount",default=0.5,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_dirt_ao_amount)
    dirt_ao_depth: FloatProperty(name="Depth",default=0.5,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_dirt_ao_depth)
    dirt_ao_distance: FloatProperty(name="Distance",default=0.5,min=0.0,max=2.0,soft_min=0.0,soft_max=2.0,update=_update_dirt_ao_distance)
    dirt_color_blend: FloatProperty(name="Blending",default=1.0,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_dirt_color_blend)
    dirt_color_source: EnumProperty(
        name="Color",
        items=(("COLOR","Color","Flat color"),("TEXTURE","Texture","Mud/surface texture")),
        default="COLOR",
        update=_update_dirt_color_source
    )
    dirt_color_image: PointerProperty(name="Color Texture",type=bpy.types.Image,update=_update_dirt_color_image)
    dirt_color_tiling: FloatProperty(name="Color Tiling",default=1.0,min=0.0,max=100.0,soft_min=0.0,soft_max=10.0,update=_update_dirt_color_image)

    dirt_mapping_source: EnumProperty(
        name="Mapping",
        items=(("UV","UV","UV"),("GENERATED","Generated","Generated"),("OBJECT","Object","Object")),
        default="GENERATED",
        update=_update_dirt_mapping
    )
    dirt_color: FloatVectorProperty(
        name="Dirt Color",
        subtype="COLOR",
        size=4,
        default=(0.12,0.035,0.008,1.0),
        min=0.0,
        max=1.0,
        update=_update_dirt_color
    )
    dirt_amount: FloatProperty(name="Dirt Amount",default=5.0,min=0.0,max=5.0,soft_min=0.0,soft_max=5.0,update=_update_dirt_amount)
    dirt_roughness: FloatProperty(name="Dirt Roughness",default=0.82,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_dirt_roughness)
    dirt_noise_scale: FloatProperty(name="Noise Scale",default=5.0,min=0.0,max=100.0,soft_min=0.0,soft_max=100.0,update=_update_dirt_noise_scale)

    grunge_mapping_source: EnumProperty(
        name="Mapping",
        items=(("UV","UV","UV"),("GENERATED","Generated","Generated"),("OBJECT","Object","Object")),
        default="GENERATED",
        update=_update_grunge_mapping
    )
    grunge_alpha_mapping_source: EnumProperty(
        name="Alpha Mapping",
        items=(("UV","UV","UV"),("GENERATED","Generated","Generated"),("OBJECT","Object","Object")),
        default="GENERATED",
        update=_update_grunge_alpha_mapping
    )
    grunge_color_mapping_source: EnumProperty(
        name="Visual Mapping",
        items=(("UV","UV","UV"),("GENERATED","Generated","Generated"),("OBJECT","Object","Object")),
        default="GENERATED",
        update=_update_grunge_color_mapping
    )
    grunge_alpha_source: EnumProperty(
        name="Alpha",
        items=(("NONE","None","No alpha mask"),("NOISE","Noise","Procedural Noise"),("TEXTURE","Texture","Image Texture")),
        default="NOISE",
        update=_update_grunge_alpha_source
    )
    grunge_color_source: EnumProperty(
        name="Color",
        items=(("COLOR","Color","Flat color"),("TEXTURE","Texture","Image Texture")),
        default="COLOR",
        update=_update_grunge_color_source
    )
    grunge_alpha_image: PointerProperty(
        name="Alpha Texture",
        type=bpy.types.Image,
        update=_update_grunge_alpha_image
    )
    grunge_color_image: PointerProperty(
        name="Color Texture",
        type=bpy.types.Image,
        update=_update_grunge_color_image
    )
    grunge_alpha_tiling: FloatProperty(
        name="Alpha Tiling", default=1.0, min=0.0, max=100.0, soft_min=0.0, soft_max=10.0,
        update=_update_grunge_tiling
    )
    grunge_color_tiling: FloatProperty(
        name="Color Tiling", default=1.0, min=0.0, max=100.0, soft_min=0.0, soft_max=10.0,
        update=_update_grunge_tiling
    )

    roughness_mapping_source: EnumProperty(name="Mapping",items=(("UV","UV","UV"),("GENERATED","Generated","Generated"),("OBJECT","Object","Object")),default="GENERATED",update=_update_roughness_mapping)
    roughness_source: EnumProperty(name="Alpha",items=(("NOISE","Noise","Procedural Noise"),("IMAGE","Image","Image Texture")),default="NOISE",update=_update_roughness)
    roughness_image: PointerProperty(name="Roughness Texture",type=bpy.types.Image,update=_update_roughness)
    roughness_tiling: FloatProperty(name="Tiling",default=1.0,min=0.0,max=100.0,soft_min=0.0,soft_max=10.0,update=_update_roughness)
    roughness_noise_scale: FloatProperty(name="Scale",default=15.0,min=0.0,max=100.0,soft_min=0.0,soft_max=20.0,update=_update_roughness)
    roughness_detail: FloatProperty(name="Detail",default=5.0,min=0.0,max=100.0,soft_min=0.0,soft_max=20.0,update=_update_roughness)
    roughness_noise_roughness: FloatProperty(name="Roughness",default=0.5,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_roughness)
    roughness_lacunarity: FloatProperty(name="Lacunarity",default=2.0,min=0.0,max=100.0,soft_min=0.0,soft_max=10.0,update=_update_roughness)
    roughness_seed: FloatProperty(name="Seed",default=0.0,min=0.0,max=10.0,soft_min=0.0,soft_max=10.0,update=_update_roughness)
    roughness_distortion: FloatProperty(name="Distortion",default=0.0,min=0.0,max=100.0,soft_min=0.0,soft_max=20.0,update=_update_roughness)
    roughness_value: FloatProperty(name="Roughness",default=0.5,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_roughness)

    grunge_noise_collapsed: BoolProperty(default=False)
    grunge_mask_ramp_collapsed: BoolProperty(default=True)
    grunge_view_mask: BoolProperty(default=False)
    dirt_mask_ramp_collapsed: BoolProperty(default=True)
    dirt_view_mask: BoolProperty(default=False)
    roughness_mask_ramp_collapsed: BoolProperty(default=True)
    roughness_view_mask: BoolProperty(default=False)
    ao_mask_ramp_collapsed: BoolProperty(default=True)
    ao_view_mask: BoolProperty(default=False)
    roughness_noise_collapsed: BoolProperty(default=False)

    hsv_hue: FloatProperty(name="Hue",default=0.5,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_hsv)
    hsv_saturation: FloatProperty(name="Saturation",default=1.0,min=0.0,max=2.0,soft_min=0.0,soft_max=2.0,update=_update_hsv)
    hsv_value: FloatProperty(name="Value",default=1.0,min=0.0,max=2.0,soft_min=0.0,soft_max=2.0,update=_update_hsv)
    hsv_amount: FloatProperty(name="Amount",default=1.0,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_hsv)

    grunge_noise_scale: FloatProperty(name="Scale",default=1.3,min=0.0,max=100.0,soft_min=0.0,soft_max=20.0,update=_update_grunge_noise)
    grunge_noise_detail: FloatProperty(name="Detail",default=15.0,min=0.0,max=100.0,soft_min=0.0,soft_max=20.0,update=_update_grunge_noise)
    grunge_noise_roughness: FloatProperty(name="Roughness",default=0.6,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_grunge_noise)
    grunge_noise_lacunarity: FloatProperty(name="Lacunarity",default=2.0,min=0.0,max=100.0,soft_min=0.0,soft_max=10.0,update=_update_grunge_noise)
    grunge_noise_seed: FloatProperty(name="Seed",default=1.0,min=0.0,max=10.0,soft_min=0.0,soft_max=10.0,update=_update_grunge_noise)
    grunge_noise_distortion: FloatProperty(name="Distortion",default=0.0,min=0.0,max=100.0,soft_min=0.0,soft_max=20.0,update=_update_grunge_noise)
    grunge_noise_normalize: BoolProperty(
        name="Normalize",
        default=False,
        update=_update_grunge_noise
    )

    grunge_amount: FloatProperty(name="Grunge Amount",default=5.0,min=0.0,max=5.0,soft_min=0.0,soft_max=5.0,update=_update_grunge_amount)
    grunge_mask_contrast: FloatProperty(name="Contrast",default=0.08,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_grunge_mask_contrast)
    grunge_blending: FloatProperty(name="Blending",default=1.0,min=0.0,max=1.0,soft_min=0.0,soft_max=1.0,update=_update_grunge_blending)
    grunge_height: FloatProperty(name="Height",default=0.0,min=-1.0,max=1.0,soft_min=-1.0,soft_max=1.0,update=_update_grunge_height)




class ML_Settings(PropertyGroup):
    material_index: IntProperty(default=0, min=0)
    grunge_source_folder: StringProperty(name="Grunges / Alphas",subtype="DIR_PATH",default="")
    texture_source_folder: StringProperty(name="Textures",subtype="DIR_PATH",default="")




class ML_AddonPreferences(AddonPreferences):
    bl_idname = __name__

    grunge_source_folder: StringProperty(name="Grunge / Alpha", subtype='DIR_PATH', default="")
    texture_source_folder: StringProperty(name="Textures", subtype='DIR_PATH', default="")

    def draw(self, context):
        layout=self.layout
        box=layout.box()
        box.label(text="Material Lab • Resource Folders",icon='FILE_FOLDER')
        box.label(text="Subfolders are searched too.")
        box.prop(self,"grunge_source_folder",text="Grunge / Alpha")
        box.prop(self,"texture_source_folder",text="Textures")


# ------------------------------------------------------------------------
# Operators
# ------------------------------------------------------------------------

class ML_OT_save_material(Operator):
    bl_idname = "material_lab.save_material"
    bl_label = "Save Material"
    bl_description = "Saves a copy of the active object's material to the library"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object

        if obj is None:
            self.report({'WARNING'}, "There is no active object.")
            return {'CANCELLED'}

        if not hasattr(obj.data, "materials"):
            self.report({'WARNING'}, "The active object does not support materials.")
            return {'CANCELLED'}

        if len(obj.data.materials) == 0 or obj.active_material is None:
            self.report({'WARNING'}, "The active object has no material.")
            return {'CANCELLED'}

        source = obj.active_material
        copy = source.copy()
        base_name = source.name
        copy.name = base_name + "_LIB"

        self.report({'INFO'}, f"Material saved: {copy.name}")
        return {'FINISHED'}


class ML_OT_apply_material(Operator):
    bl_idname = "material_lab.apply_material"
    bl_label = "Apply Material"
    bl_description = "Applies the selected material to the active object"
    bl_options = {'REGISTER', 'UNDO'}

    material_name: StringProperty()

    def execute(self, context):
        mat = bpy.data.materials.get(self.material_name)
        obj = context.active_object

        if mat is None:
            self.report({'WARNING'}, "Material not found.")
            return {'CANCELLED'}

        if obj is None or not hasattr(obj.data, "materials"):
            self.report({'WARNING'}, "The active object does not support materials.")
            return {'CANCELLED'}

        if len(obj.data.materials) == 0:
            obj.data.materials.append(mat)
        else:
            slot = obj.active_material_index
            if slot >= len(obj.data.materials):
                obj.data.materials.append(mat)
            else:
                obj.data.materials[slot] = mat

        self.report({'INFO'}, f"Aplicado: {mat.name}")
        return {'FINISHED'}


def _copy_material_lab_settings(src_mat,dst_mat):
    try:
        src=src_mat.material_lab_settings
        dst=dst_mat.material_lab_settings

        for prop in src.bl_rna.properties:
            name=prop.identifier
            if name=="rna_type" or prop.is_readonly or prop.type=="COLLECTION":
                continue
            try:
                setattr(dst,name,getattr(src,name))
            except Exception:
                pass

        dst.mixer_layers.clear()
        for src_layer in src.mixer_layers:
            dst_layer=dst.mixer_layers.add()
            for prop in src_layer.bl_rna.properties:
                name=prop.identifier
                if name=="rna_type" or prop.is_readonly or prop.type=="COLLECTION":
                    continue
                try:
                    setattr(dst_layer,name,getattr(src_layer,name))
                except Exception:
                    pass
    except Exception:
        pass


class ML_OT_duplicate_material(Operator):
    bl_idname = "material_lab.duplicate_material"
    bl_label = "Duplicar"
    bl_description = "Creates an independent copy and assigns it to the active slot"
    bl_options = {'REGISTER', 'UNDO'}

    material_name: StringProperty()

    def execute(self, context):
        obj=context.active_object
        mat=bpy.data.materials.get(self.material_name)
        if mat is None:
            self.report({'WARNING'},"Material not found.")
            return {'CANCELLED'}

        copy=mat.copy()
        copy.name=mat.name
        try:
            copy.use_fake_user=False
        except Exception:
            pass
        _copy_material_lab_settings(mat,copy)

        if obj is not None and hasattr(obj.data,"materials"):
            idx=obj.active_material_index
            if idx < len(obj.data.materials):
                obj.data.materials[idx]=copy



        self.report({'INFO'},f"Duplicado y asignado: {copy.name}")
        return {'FINISHED'}



class ML_OT_delete_material(Operator):
    bl_idname = "material_lab.delete_material"
    bl_label = "Delete"
    bl_description = "Deletes the selected material from Blender"
    bl_options = {'REGISTER', 'UNDO'}

    material_name: StringProperty()

    def execute(self, context):
        mat = bpy.data.materials.get(self.material_name)

        if mat is None:
            self.report({'WARNING'}, "Material not found.")
            return {'CANCELLED'}

        bpy.data.materials.remove(mat)
        self.report({'INFO'}, "Material deleted.")
        return {'FINISHED'}


def get_library_path(context):
    path = context.scene.material_lab_settings.library_path
    return bpy.path.abspath(path) if path else ""


def ensure_asset_library(path):
    """Register path as a Blender Asset Library if possible."""
    if not path:
        return False

    path = os.path.abspath(path)

    # Avoid duplicate entries.
    for lib in bpy.context.preferences.filepaths.asset_libraries:
        try:
            if os.path.abspath(bpy.path.abspath(lib.path)) == path:
                return True
        except Exception:
            pass

    try:
        name = os.path.basename(path.rstrip(os.sep)) or "Material Lab"
        bpy.context.preferences.filepaths.asset_libraries.new(name=name, path=path)
        return True
    except Exception:
        return False


class ML_OT_choose_library_folder(Operator):
    bl_idname = "material_lab.choose_library_folder"
    bl_label = "Choose Folder"
    bl_description = "Choose where to save materials as assets"

    directory: StringProperty(subtype='DIR_PATH')

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.directory:
            return {'CANCELLED'}

        path = os.path.abspath(bpy.path.abspath(self.directory))
        os.makedirs(path, exist_ok=True)

        context.scene.material_lab_settings.library_path = path
        ensure_asset_library(path)

        self.report({'INFO'}, f"Biblioteca: {path}")
        return {'FINISHED'}


class ML_OT_save_material_asset(Operator):
    bl_idname = "material_lab.save_material_asset"
    bl_label = "Save as Asset"
    bl_description = "Saves the active material to the selected library and registers it as an Asset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        mat = obj.active_material if obj else None
        path = get_library_path(context)

        if obj is None or mat is None:
            self.report({'WARNING'}, "The active object has no material.")
            return {'CANCELLED'}

        if not path:
            self.report({'WARNING'}, "Choose a library folder first.")
            return {'CANCELLED'}

        os.makedirs(path, exist_ok=True)
        ensure_asset_library(path)

        # Create a clean copy so the library asset is independent.
        asset_mat = mat.copy()
        safe_name = bpy.path.clean_name(asset_mat.name)
        if not safe_name:
            safe_name = "Material"
        filepath = os.path.join(path, safe_name + ".blend")

        # Avoid overwriting the current Blender file accidentally.
        current = bpy.path.abspath(bpy.data.filepath)
        if current and os.path.abspath(filepath) == os.path.abspath(current):
            filepath = os.path.join(path, safe_name + "_Asset.blend")

        try:
            # Mark as an Asset before writing.
            asset_mat.asset_mark()
        except Exception:
            # Older/limited builds may expose asset marking differently.
            pass

        try:
            asset_mat.use_fake_user = True
            bpy.data.libraries.write(
                filepath,
                {asset_mat},
                fake_user=True,
                path_remap='RELATIVE'
            )
        except Exception as exc:
            bpy.data.materials.remove(asset_mat)
            self.report({'ERROR'}, f"Could not save the asset: {exc}")
            return {'CANCELLED'}

        bpy.data.materials.remove(asset_mat)

        self.report({'INFO'}, f"Asset saved: {os.path.basename(filepath)}")
        return {'FINISHED'}


class ML_OT_open_asset_browser(Operator):
    bl_idname = "material_lab.open_asset_browser"
    bl_label = "Open Asset Browser"
    bl_description = "Changes the current area to the Asset Browser"

    def execute(self, context):
        try:
            context.area.type = 'FILE_BROWSER'
            context.area.spaces.active.browse_mode = 'ASSETS'
            return {'FINISHED'}
        except Exception as exc:
            self.report({'WARNING'}, f"Could not open the Asset Browser here: {exc}")
            return {'CANCELLED'}


class ML_OT_rename_material(Operator):
    bl_idname = "material_lab.rename_material"
    bl_label = "Rename Material"
    bl_description = "Renames the selected material"
    bl_options = {'REGISTER', 'UNDO'}

    material_name: StringProperty()
    new_name: StringProperty(name="Nuevo nombre")

    def invoke(self, context, event):
        mat = bpy.data.materials.get(self.material_name)
        if mat is None:
            return {'CANCELLED'}
        self.new_name = mat.name
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        mat = bpy.data.materials.get(self.material_name)

        if mat is None:
            self.report({'WARNING'}, "Material not found.")
            return {'CANCELLED'}

        name = self.new_name.strip()
        if not name:
            self.report({'WARNING'}, "The name cannot be empty.")
            return {'CANCELLED'}

        mat.name = name
        self.report({'INFO'}, f"Material renamed: {mat.name}")
        return {'FINISHED'}


class ML_OT_assign_material(Operator):
    bl_idname = "material_lab.assign_material"
    bl_label = "Asignar a caras"
    bl_description = "Assigns this material to the selected faces in Edit Mode"
    bl_options = {'REGISTER', 'UNDO'}

    material_name: StringProperty()

    def execute(self, context):
        obj = context.active_object
        mat = bpy.data.materials.get(self.material_name)

        if obj is None or obj.type != 'MESH':
            self.report({'WARNING'}, "You need an active Mesh object.")
            return {'CANCELLED'}

        if mat is None:
            self.report({'WARNING'}, "Material not found.")
            return {'CANCELLED'}

        if context.mode != 'EDIT_MESH':
            self.report({'WARNING'}, "Enter Edit Mode and select the faces first.")
            return {'CANCELLED'}

        # Make sure the material exists in the object's slots.
        slot_index = None
        for i, slot in enumerate(obj.material_slots):
            if slot.material == mat:
                slot_index = i
                break

        if slot_index is None:
            obj.data.materials.append(mat)
            slot_index = len(obj.material_slots) - 1

        obj.active_material_index = slot_index

        import bmesh
        bm = bmesh.from_edit_mesh(obj.data)
        count = 0
        for face in bm.faces:
            if face.select:
                face.material_index = slot_index
                count += 1

        bmesh.update_edit_mesh(obj.data)

        self.report({'INFO'}, f"{count} face(s) assigned with {mat.name}")
        return {'FINISHED'}


class ML_OT_create_material(Operator):
    bl_idname = "material_lab.create_material"
    bl_label = "New Material"
    bl_description = "Creates a new material and assigns it to the active object"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object

        if obj is None or not hasattr(obj.data, "materials"):
            self.report({'WARNING'}, "Select an object that supports materials.")
            return {'CANCELLED'}

        if obj.active_material:
            source=obj.active_material
            mat=source.copy()
            mat.name=source.name
            try:
                mat.use_fake_user=False
            except Exception:
                pass
            _copy_material_lab_settings(source,mat)
            message="Material duplicated"
        else:
            mat=bpy.data.materials.new(name="MAT_New")
            ensure_material_node_setup(mat)
            message="Material created"

        if len(obj.data.materials)==0:
            obj.data.materials.append(mat)
        else:
            obj.data.materials[obj.active_material_index]=mat

        self.report({'INFO'},f"{message}: {mat.name}")
        return {'FINISHED'}


# ------------------------------------------------------------------------
# UI
# ------------------------------------------------------------------------

class ML_UL_materials(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        mat = item

        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text=mat.name, icon='MATERIAL')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='MATERIAL')


# ------------------------------------------------------------------------
# Aspect / node operators
# ------------------------------------------------------------------------

def _unique_node_name(mat, base):
    nodes = mat.node_tree.nodes
    if base not in nodes:
        return base
    i = 2
    while f"{base}_{i}" in nodes:
        i += 1
    return f"{base}_{i}"



_EFFECT_UI_DEFAULT_ORDER = ("Grunge", "Dirt", "Roughness", "AO", "HSV")


def _effect_ui_order(mat):
    if not mat:
        return list(_EFFECT_UI_DEFAULT_ORDER)
    raw = mat.get("ML_EFFECT_UI_ORDER", "")
    order = [item for item in raw.split("|") if item in _EFFECT_UI_DEFAULT_ORDER]
    for effect in _EFFECT_UI_DEFAULT_ORDER:
        if effect not in order:
            order.append(effect)
    return order


def _save_effect_ui_order(mat, order):
    if mat:
        mat["ML_EFFECT_UI_ORDER"] = "|".join(
            effect for effect in order if effect in _EFFECT_UI_DEFAULT_ORDER
        )


def _effect_group_collapsed(frame, group):
    if not frame:
        return False
    return bool(frame.get(f"ML_GROUP_{group}_COLLAPSED", False))


def _effect_nodes(mat,effect):
    if not mat or not mat.node_tree:
        return []
    prefix=f"ML_{effect}"
    return [
        n for n in mat.node_tree.nodes
        if n.name==prefix
        or n.name.startswith(prefix+"_")
        or n.get("ML_EFFECT")==effect
        or n.get("ML_effect")==effect
    ]




def _capture_socket(sock):
    if sock and sock.is_linked and sock.links:
        return sock.links[0].from_socket
    return None


def _store_original_surface(frame, output_socket):
    if not frame or not output_socket:
        return
    source = _capture_socket(output_socket)
    frame["ML_ORIG_SURFACE_NODE"] = source.node.name if source else ""
    frame["ML_ORIG_SURFACE_SOCKET"] = source.name if source else ""


def _store_original_input(frame, key, socket):
    source = _capture_socket(socket)
    frame[f"ML_ORIG_{key}_NODE"] = source.node.name if source else ""
    frame[f"ML_ORIG_{key}_SOCKET"] = source.name if source else ""
    try:
        frame[f"ML_ORIG_{key}_DEFAULT"] = list(socket.default_value)
    except Exception:
        try:
            frame[f"ML_ORIG_{key}_DEFAULT"] = float(socket.default_value)
        except Exception:
            frame[f"ML_ORIG_{key}_DEFAULT"] = 0.0


def _resolve_saved_source(mat, frame, key, visiting=None):
    if visiting is None:
        visiting=set()
    if not frame or frame.name in visiting:
        return None,None
    visiting.add(frame.name)

    node_name=frame.get(f"ML_ORIG_{key}_NODE","")
    sock_name=frame.get(f"ML_ORIG_{key}_SOCKET","")
    nodes=mat.node_tree.nodes
    node=nodes.get(node_name) if node_name else None

    if node:
        sock=node.outputs.get(sock_name) if sock_name else None
        return node,sock

    # The source node may belong to an effect that was removed earlier.
    # Its downstream frame should already have been repaired; fall back to
    # the current frame's stored default when no live source remains.
    return None,None


def _repair_downstream_effect_sources(mat, removed_group, removed_frame):
    nodes=mat.node_tree.nodes
    removed_names={n.name for n in removed_group}

    # The removed effect may be referenced by later effects' saved original
    # inputs. Replace those saved references now, before deleting the group.
    for frame in [n for n in nodes if n.type=="FRAME" and n!=removed_frame and n.name.startswith("ML_")]:
        effect=frame.get("ML_EFFECT") or frame.label
        if not effect:
            continue

        for key in ("BASE_COLOR","ROUGHNESS","NORMAL"):
            ref=frame.get(f"ML_ORIG_{key}_NODE","")
            if ref not in removed_names:
                continue

            resolved_node,resolved_socket=_resolve_saved_source(mat,removed_frame,key)
            if resolved_node and resolved_socket:
                frame[f"ML_ORIG_{key}_NODE"]=resolved_node.name
                frame[f"ML_ORIG_{key}_SOCKET"]=resolved_socket.name
                continue

            default=removed_frame.get(f"ML_ORIG_{key}_DEFAULT")
            if default is not None:
                frame[f"ML_ORIG_{key}_NODE"]=""
                frame[f"ML_ORIG_{key}_SOCKET"]=""
                frame[f"ML_ORIG_{key}_DEFAULT"]=default


def _remove_effect_nodes(mat,effect):
    if not mat or not mat.node_tree:
        return False

    nodes=mat.node_tree.nodes
    links=mat.node_tree.links
    group=_effect_nodes(mat,effect)
    if not group:
        return False

    group_set=set(group)
    frame=next((n for n in group if n.type=="FRAME"),None)
    if not frame:
        return False

    # Repair all later effects before anything is deleted.
    _repair_downstream_effect_sources(mat,group,frame)

    reconnect=[]

    def resolve_original(key):
        node_name=frame.get(f"ML_ORIG_{key}_NODE","")
        sock_name=frame.get(f"ML_ORIG_{key}_SOCKET","")
        source=nodes.get(node_name) if node_name else None
        if source:
            sock=source.outputs.get(sock_name) if sock_name else None
            if sock:
                return source,sock
        return None,None

    # Snapshot every destination BEFORE removing a NodeLink.
    for node in [n for n in group if n.get("ML_FINAL")]:
        key=node.get("ML_FINAL")
        source_node,source_socket=resolve_original(key)

        for output in node.outputs:
            for link in list(output.links):
                to_node=link.to_node
                to_socket=link.to_socket
                if to_node not in group_set:
                    links.remove(link)
                    reconnect.append(
                        (source_socket,to_node,to_socket,key,frame.get(f"ML_ORIG_{key}_DEFAULT"))
                    )

    # Fallback for older effects without ML_FINAL.
    if not any(n.get("ML_FINAL") for n in group):
        bsdf=get_existing_principled(mat)
        if bsdf:
            for key,socket_name in (
                ("BASE_COLOR","Base Color"),
                ("ROUGHNESS","Roughness"),
                ("NORMAL","Normal"),
            ):
                sock=bsdf.inputs.get(socket_name)
                if not sock:
                    continue
                for link in list(sock.links):
                    if link.from_node in group_set:
                        to_node=link.to_node
                        to_socket=link.to_socket
                        links.remove(link)
                        source_node,source_socket=resolve_original(key)
                        reconnect.append(
                            (source_socket,to_node,to_socket,key,frame.get(f"ML_ORIG_{key}_DEFAULT"))
                        )

    # Delete the complete group, including Frame.
    for node in list(group):
        if node.name in nodes:
            nodes.remove(node)

    # Reconnect saved sources after deletion.
    for source,to_node,to_socket,key,default in reconnect:
        if source and source.id_data==mat.node_tree:
            try:
                links.new(source,to_socket)
                continue
            except Exception:
                pass
        if default is not None:
            try:
                to_socket.default_value=default
            except Exception:
                pass

    return True



def _find_effect_frame(mat, effect):
    if not mat or not mat.node_tree:
        return None
    return next(
        (n for n in mat.node_tree.nodes
         if n.type == "FRAME" and n.name.startswith(f"ML_{effect}_")),
        None
    )


def _find_effect_node(mat, effect, role=None, node_type=None):
    group = _effect_nodes(mat, effect)
    for node in group:
        if role is not None and node.get("ML_ROLE") != role:
            continue
        if node_type is not None and node.type != node_type:
            continue
        return node
    return None


def _layout_new_effect(mat,effect):
    nodes=mat.node_tree.nodes
    group=_effect_nodes(mat,effect)
    if not group:
        return
    frame=next((n for n in group if n.type=="FRAME"),None)
    if not frame:
        return

    existing_frames=[n for n in nodes if n.type=="FRAME" and n!=frame and n.name.startswith("ML_")]
    effect_frames=[n for n in existing_frames if n.get("ML_EFFECT")]
    source_nodes=[
        n for n in nodes
        if n != frame and n.type != "FRAME"
        and not n.get("ML_EFFECT") and not n.get("ML_GLOBAL_MAPPING")
        and n.type not in {"OUTPUT_MATERIAL","BSDF_PRINCIPLED"}
    ]
    if source_nodes:
        min_x=min(float(n.location.x) for n in source_nodes)
        min_y=min(float(n.location.y) for n in source_nodes)
    else:
        min_x=-400.0
        min_y=0.0
    frame.location=(min_x-300.0, min_y-950.0-(len(effect_frames)*1150.0))

    frame_colors={
        "Grunge":(0.34,0.16,0.05),
        "Dirt":(0.42,0.22,0.05),
        "Roughness":(0.12,0.34,0.10),
        "Material Mixer":(0.08,0.25,0.42),
        "HSV":(0.42,0.08,0.55),
        "AO":(0.16,0.28,0.46),
    }
    frame.use_custom_color=True
    frame.color=frame_colors.get(effect,(0.25,0.25,0.25))
    frame.label="Color" if effect=="HSV" else effect

    if effect=="Material Mixer":
        positions={"Mix":(900,0)}
    elif effect=="Grunge":
        # Deliberately separated lanes: MASK on top, DIFFUSE in the middle,
        # NORMAL/output on the bottom. Large gaps prevent visual overlap.
        positions={
            "AlphaCoord":(0,420),
            "AlphaMapping":(300,420),
            "AlphaNoise":(600,620),
            "AlphaImage":(600,390),
            "AlphaRamp":(900,520),
            "Amount":(1180,520),

            "ColorCoord":(0,-120),
            "ColorMapping":(300,-120),
            "Color":(600,-80),
            "ColorImage":(600,-350),
            "ColorMix":(900,-80),
            "Mix":(1230,80),

            "HeightInvert":(1010,-300),
            "Bump":(1330,-300),
        }
    elif effect=="Roughness":
        positions={
            "Coord":(0,220),
            "Mapping":(300,220),
            "Noise":(600,390),
            "Image":(600,140),
            "Mask":(900,300),
            "Amount":(1180,300),
            "Mix":(1480,180),
        }
    else:
        positions={
            "Coord":(0,220),
            "Mapping":(300,220),
            "Texture":(600,220),
            "OptionalImage":(600,-20),
            "Ramp":(900,220),
            "AO":(600,-250),
            "Color":(900,-20),
            "ColorMix":(1180,-20),
            "Amount":(1180,-260),
            "Blending":(1180,220),
            "AmountRoughness":(1180,-500),
            "ColorBlend":(900,-20),
            "ColorMapping":(300,-220),
            "ColorImage":(600,-500),
            "Mix":(1500,40),
            "RoughnessMix":(1500,-220),
            "Bump":(1500,-460),
            "HSV":(900,220),
            "Curves":(1220,220),
            "Depth":(1180,-500),
        }

    counters={}
    effect_nodes=[n for n in group if n.type!="FRAME"]

    # First place nodes at their intended anchor positions.
    for node in effect_nodes:
        role=node.get("ML_ROLE","")
        pos=positions.get(role)
        if pos is None:
            idx=counters.get(node.type,0)
            pos=(1000,720-idx*220)
            counters[node.type]=idx+1
        node.location=pos

    # Then resolve any actual rectangle overlaps. Blender node dimensions can
    # vary by version/content, so use the real dimensions with conservative
    # minimums and a generous gap. This preserves the general layout while
    # guaranteeing that nodes do not visually sit on top of one another.
    gap_x=120.0
    gap_y=80.0
    placed=[]
    for node in sorted(effect_nodes, key=lambda n:(-float(n.location.y), float(n.location.x), n.name)):
        x=float(node.location.x)
        y=float(node.location.y)
        w=max(float(node.dimensions.x),180.0)
        h=max(float(node.dimensions.y),100.0)

        moved=True
        guard=0
        while moved and guard<100:
            moved=False
            guard+=1
            for other in placed:
                ox=float(other.location.x)
                oy=float(other.location.y)
                ow=max(float(other.dimensions.x),180.0)
                oh=max(float(other.dimensions.y),100.0)

                overlap_x=(x < ox+ow+gap_x) and (x+w+gap_x > ox)
                overlap_y=(y < oy+oh+gap_y) and (y+h+gap_y > oy)
                if overlap_x and overlap_y:
                    # Move right of the conflicting node. Keep the intended
                    # vertical lane instead of collapsing everything into one row.
                    x=ox+ow+gap_x
                    moved=True
        node.location=(x,y)
        placed.append(node)


def _new_frame(mat,effect):
    frame=mat.node_tree.nodes.new("NodeFrame")
    frame.name=_unique_node_name(mat,f"ML_{effect}_Frame")
    frame.label=effect
    frame.label_size=28
    frame["ML_EFFECT"]=effect
    frame["ML_effect"]=effect
    frame["ML_ENABLED"]=True
    frame["ML_COLLAPSED"]=False

    # UX defaults: open the first useful section, keep secondary sections
    # compact. These flags affect only the UI, never the shader graph.
    defaults={
        "Grunge": {"MASK": False, "DIFFUSE": True, "HEIGHT": True},
        "Dirt": {"MASK": False, "VISUAL": True, "MAPPING": True},
        "Roughness": {"MASK": False, "VISUAL": True, "MAPPING": True},
        "AO": {"MASK": True, "VISUAL": False},
        "HSV": {"VISUAL": False},
    }
    for group,collapsed in defaults.get(effect,{}).items():
        frame[f"ML_GROUP_{group}_COLLAPSED"]=collapsed

    return frame


class ML_OT_set_active_slot(Operator):
    bl_idname = "material_lab.set_active_slot"
    bl_label = "Activate Slot"
    bl_options = {'REGISTER', 'UNDO'}

    slot_index: IntProperty(min=0)

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return {'CANCELLED'}
        if self.slot_index >= len(obj.material_slots):
            return {'CANCELLED'}
        obj.active_material_index = self.slot_index
        return {'FINISHED'}


class ML_OT_remove_slot(Operator):
    bl_idname = "material_lab.remove_slot"
    bl_label = "Quitar slot"
    bl_options = {'REGISTER', 'UNDO'}

    slot_index: IntProperty(min=0)

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return {'CANCELLED'}
        if self.slot_index >= len(obj.material_slots):
            return {'CANCELLED'}
        obj.active_material_index = self.slot_index
        return bpy.ops.object.material_slot_remove()


class ML_OT_effect_texture_source(Operator):
    bl_idname = "material_lab.effect_texture_source"
    bl_label = "Texture Source"
    bl_options = {'REGISTER', 'UNDO'}

    effect: StringProperty()
    source: StringProperty()

    def execute(self, context):
        obj = context.active_object
        mat = obj.active_material if obj else None
        if not mat or not mat.node_tree:
            return {'CANCELLED'}

        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        frame = _find_effect_frame(mat, self.effect)
        if not frame:
            return {'CANCELLED'}

        coord = _find_effect_node(mat, self.effect, "Coord")
        mapping = _find_effect_node(mat, self.effect, "Mapping")
        image = _find_effect_node(mat, self.effect, "OptionalImage", "TEX_IMAGE")
        noise = _find_effect_node(mat, self.effect, "Texture", "TEX_NOISE")

        # UV / Generated applies to the shared Mapping node.
        if self.source in {"UV", "GENERATED", "OBJECT"}:
            if not coord or not mapping:
                self.report({'WARNING'}, "This effect has no configurable coordinates.")
                return {'CANCELLED'}

            for link in list(mapping.inputs["Vector"].links):
                links.remove(link)

            socket_name = (
                "UV" if self.source == "UV"
                else "Object" if self.source == "OBJECT"
                else "Generated"
            )
            socket = coord.outputs.get(socket_name)
            if socket:
                links.new(socket, mapping.inputs["Vector"])
                frame["ML_VECTOR_SOURCE"] = self.source
                self.report({'INFO'}, f"{self.effect}: vector {self.source}.")
                return {'FINISHED'}

        # Image / Noise switches the actual mask source.
        if self.source in {"IMAGE", "NOISE"}:
            if self.source == "IMAGE":
                if not image or not image.image:
                    self.report({'WARNING'}, "Choose an image first.")
                    return {'CANCELLED'}

            if self.effect == "Dirt":
                target = next(
                    (n for n in _effect_nodes(mat, "Dirt")
                     if n.type == "VALTORGB" and "Noise_Ramp" in n.name),
                    None
                )
            else:
                target = _find_effect_node(mat, self.effect, "Ramp", "VALTORGB")
            if not target:
                self.report({'WARNING'}, "Texture mask not found.")
                return {'CANCELLED'}

            target_input = target.inputs.get("Fac")
            if not target_input:
                return {'CANCELLED'}

            for link in list(target_input.links):
                links.remove(link)

            if self.source == "IMAGE":
                links.new(image.outputs["Color"], target_input)
            else:
                if not noise:
                    self.report({'WARNING'}, "This effect has no Noise.")
                    return {'CANCELLED'}
                links.new(noise.outputs["Fac"], target_input)

            frame["ML_TEXTURE_SOURCE"] = self.source
            self.report({'INFO'}, f"{self.effect}: {self.source.lower()} conectado.")
            return {'FINISHED'}

        # Grunge has an extra Curves node and mixes its mask directly.
        if self.source == "IMAGE" and self.effect == "Grunge":
            return {'FINISHED'}

        return {'CANCELLED'}


class ML_OT_grunge_image_source(Operator):
    bl_idname="material_lab.grunge_image_source"
    bl_label="Grunge Source"
    bl_options={'REGISTER','UNDO'}

    source:StringProperty()

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat or not mat.node_tree:
            return {'CANCELLED'}

        links=mat.node_tree.links
        image=_find_effect_node(mat,"Grunge","OptionalImage","TEX_IMAGE")
        noise=_find_effect_node(mat,"Grunge","Texture","TEX_NOISE")
        ramp=_find_effect_node(mat,"Grunge","Ramp")
        frame=_find_effect_frame(mat,"Grunge")

        if not image or not noise or not ramp:
            return {'CANCELLED'}

        target=ramp.inputs.get("Fac")
        if not target:
            return {'CANCELLED'}

        for link in list(target.links):
            links.remove(link)

        if self.source=="IMAGE":
            if not image.image:
                self.report({'WARNING'},"Choose an image first.")
                return {'CANCELLED'}
            links.new(image.outputs["Color"],target)
            if frame:
                frame["ML_TEXTURE_SOURCE"]="IMAGE"
            self.report({'INFO'},"Grunge: image connected.")
            return {'FINISHED'}

        links.new(noise.outputs["Fac"],target)
        if frame:
            frame["ML_TEXTURE_SOURCE"]="NOISE"
        self.report({'INFO'},"Grunge: Noise connected.")
        return {'FINISHED'}

class ML_OT_grunge_mapping_source(Operator):
    bl_idname="material_lab.grunge_mapping_source"
    bl_label="Grunge Mapping"
    bl_options={'REGISTER','UNDO'}

    source:StringProperty()

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat:
            return {'CANCELLED'}
        _set_effect_mapping_source(mat,"Grunge",self.source)
        mat.material_lab_settings.grunge_mapping_source=self.source
        frame=_find_effect_frame(mat,"Grunge")
        if frame:
            frame["ML_MAPPING_SOURCE"]=self.source
        return {'FINISHED'}




class ML_OT_grunge_alpha_source(Operator):
    bl_idname="material_lab.grunge_alpha_source"
    bl_label="Grunge Mask Source"
    bl_options={'REGISTER','UNDO'}

    source:StringProperty()

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat:
            return {'CANCELLED'}
        mat.material_lab_settings.grunge_alpha_source=self.source
        _update_grunge_alpha_source(mat.material_lab_settings,context)
        frame=_find_effect_frame(mat,"Grunge")
        if frame:
            frame["ML_ALPHA_SOURCE"]=self.source
        return {'FINISHED'}


class ML_OT_grunge_color_source(Operator):
    bl_idname="material_lab.grunge_color_source"
    bl_label="Grunge Diffuse Source"
    bl_options={'REGISTER','UNDO'}

    source:StringProperty()

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat:
            return {'CANCELLED'}
        mat.material_lab_settings.grunge_color_source=self.source
        _update_grunge_color_source(mat.material_lab_settings,context)
        frame=_find_effect_frame(mat,"Grunge")
        if frame:
            frame["ML_COLOR_SOURCE"]=self.source
        return {'FINISHED'}







class ML_OT_reset_material_tiling(Operator):
    bl_idname="material_lab.reset_material_tiling"
    bl_label="Reset Tiling"
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat:
            return {'CANCELLED'}
        mat.material_lab_settings.global_tiling=1.0
        mat.material_lab_settings.global_coordinate_source="DEFAULT"
        _restore_base_mapping_state(mat)
        _remove_global_mapping_nodes(mat)
        self.report({'INFO'},"Material tiling reset to 1.")
        return {'FINISHED'}

class ML_OT_toggle_collapse(Operator):
    bl_idname="material_lab.toggle_collapse"
    bl_label="Expand / Collapse Effect"
    bl_options={'REGISTER','UNDO'}

    effect:StringProperty()

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat:
            return {'CANCELLED'}
        frame=_find_effect_frame(mat,self.effect)
        if not frame:
            return {'CANCELLED'}
        frame["ML_COLLAPSED"]=not bool(frame.get("ML_COLLAPSED",False))
        return {'FINISHED'}



class ML_OT_toggle_effect_group(Operator):
    bl_idname="material_lab.toggle_effect_group"
    bl_label="Expand / Collapse Effect Group"
    bl_options={'REGISTER','UNDO'}

    effect:StringProperty()
    group:StringProperty()

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat:
            return {'CANCELLED'}
        frame=_find_effect_frame(mat,self.effect)
        if not frame:
            return {'CANCELLED'}
        key=f"ML_GROUP_{self.group.upper()}_COLLAPSED"
        frame[key]=not bool(frame.get(key,False))
        return {'FINISHED'}


class ML_OT_collapse_all_effects(Operator):
    bl_idname="material_lab.collapse_all_effects"
    bl_label="Collapse All"
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat or not mat.node_tree:
            return {'CANCELLED'}
        for node in mat.node_tree.nodes:
            if node.type=="FRAME" and node.name.startswith("ML_") and node.get("ML_EFFECT"):
                node["ML_COLLAPSED"]=True
        return {'FINISHED'}


class ML_OT_move_effect(Operator):
    bl_idname="material_lab.move_effect"
    bl_label="Move Effect"
    bl_options={'REGISTER','UNDO'}

    effect:StringProperty()
    direction:IntProperty(default=1)

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat:
            return {'CANCELLED'}

        order=[e for e in _effect_ui_order(mat) if _effect_nodes(mat,e)]
        if self.effect not in order:
            return {'CANCELLED'}

        idx=order.index(self.effect)
        ni=idx+self.direction
        if not (0 <= ni < len(order)):
            return {'CANCELLED'}

        other=order[ni]
        frame_a=_find_effect_frame(mat,self.effect)
        frame_b=_find_effect_frame(mat,other)
        if not frame_a or not frame_b:
            return {'CANCELLED'}

        # Swap frame positions only. The node graph is left untouched.
        pos_a=frame_a.location.copy()
        pos_b=frame_b.location.copy()
        frame_a.location=pos_b
        frame_b.location=pos_a

        order[idx],order[ni]=order[ni],order[idx]
        _save_effect_ui_order(mat, order)
        return {'FINISHED'}



def _effect_final_socket(node):
    if not node:
        return None
    if node.get("ML_FINAL")=="BASE_COLOR":
        return node.outputs.get("Color") or node.outputs.get("Value")
    if node.get("ML_FINAL")=="ROUGHNESS":
        return node.outputs.get("Color") or node.outputs.get("Value")
    if node.get("ML_FINAL")=="NORMAL":
        return node.outputs.get("Normal")
    return None


def _saved_input_source(mat, frame, key):
    nodes=mat.node_tree.nodes
    node_name=frame.get(f"ML_ORIG_{key}_NODE","")
    sock_name=frame.get(f"ML_ORIG_{key}_SOCKET","")
    source_node=nodes.get(node_name) if node_name else None
    source_socket=source_node.outputs.get(sock_name) if source_node and sock_name else None
    return source_socket


def _apply_saved_input_to_socket(mat, frame, key, target_socket):
    if not mat or not frame or not target_socket:
        return False
    links=mat.node_tree.links

    for link in list(target_socket.links):
        links.remove(link)

    source_socket=_saved_input_source(mat,frame,key)
    if source_socket:
        try:
            links.new(source_socket,target_socket)
            return True
        except Exception:
            pass

    default=frame.get(f"ML_ORIG_{key}_DEFAULT")
    if default is not None:
        try:
            target_socket.default_value=default
            return True
        except Exception:
            pass
    return False


def _effect_bypass(mat, effect, enabled):
    if not mat or not mat.node_tree:
        return False

    frame=_find_effect_frame(mat,effect)
    if not frame or effect=="Material Mixer":
        return False

    nodes=mat.node_tree.nodes
    links=mat.node_tree.links
    group_set=set(_effect_nodes(mat,effect))
    final_nodes=[n for n in group_set if n.get("ML_FINAL")]

    if enabled:
        # Restore the exact external destinations saved when the effect
        # was bypassed. This can be the Principled BSDF or another effect.
        for node in final_nodes:
            key=node.get("ML_FINAL")
            source=_effect_final_socket(node)
            if not source:
                continue

            raw=frame.get(f"ML_BYPASS_TARGETS_{key}","")
            targets=[]
            if raw:
                try:
                    targets=json.loads(raw)
                except Exception:
                    targets=[]

            for target in targets:
                to_node=nodes.get(target.get("node",""))
                to_socket=to_node.inputs.get(target.get("socket","")) if to_node else None
                if not to_socket:
                    continue
                for link in list(to_socket.links):
                    links.remove(link)
                try:
                    links.new(source,to_socket)
                except Exception:
                    pass

            if not targets:
                bsdf=get_existing_principled(mat)
                input_name={
                    "BASE_COLOR":"Base Color",
                    "ROUGHNESS":"Roughness",
                    "NORMAL":"Normal",
                }.get(key)
                target=bsdf.inputs.get(input_name) if bsdf and input_name else None
                if target:
                    for link in list(target.links):
                        links.remove(link)
                    try:
                        links.new(source,target)
                    except Exception:
                        pass

            frame[f"ML_BYPASS_TARGETS_{key}"]=""

        frame["ML_ENABLED"]=True
        return True

    # Disable: snapshot every destination of each final output, replace those
    # links with the exact pre-effect source, and keep the effect graph intact.
    for node in final_nodes:
        key=node.get("ML_FINAL")
        source=_effect_final_socket(node)
        if not source:
            continue

        targets=[]
        for link in list(source.links):
            if link.to_node in group_set:
                continue
            targets.append({
                "node":link.to_node.name,
                "socket":link.to_socket.name,
            })

        frame[f"ML_BYPASS_TARGETS_{key}"]=json.dumps(targets)

        for target in targets:
            to_node=nodes.get(target["node"])
            to_socket=to_node.inputs.get(target["socket"]) if to_node else None
            if not to_socket:
                continue
            for link in list(to_socket.links):
                links.remove(link)
            _apply_saved_input_to_socket(mat,frame,key,to_socket)

    # If there was no external link (unusual, but possible after manual edits),
    # make the Principled input itself bypass-safe.
    for key,input_name in (
        ("BASE_COLOR","Base Color"),
        ("ROUGHNESS","Roughness"),
        ("NORMAL","Normal"),
    ):
        if not any(n.get("ML_FINAL")==key for n in final_nodes):
            continue
        bsdf=get_existing_principled(mat)
        target=bsdf.inputs.get(input_name) if bsdf else None
        if target and not target.is_linked:
            _apply_saved_input_to_socket(mat,frame,key,target)

    frame["ML_ENABLED"]=False
    return True


class ML_OT_toggle_look(Operator):
    bl_idname="material_lab.toggle_look"
    bl_label="Enable / Disable Effect"
    bl_options={'REGISTER','UNDO'}

    effect:StringProperty()

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat:
            return {'CANCELLED'}

        frame=_find_effect_frame(mat,self.effect)
        if not frame:
            return {'CANCELLED'}

        enabled=not bool(frame.get("ML_ENABLED",True))

        if self.effect=="Material Mixer":
            frame["ML_ENABLED"]=enabled
            if not _rebuild_material_mixer(mat):
                return {'CANCELLED'}
            self.report({'INFO'},f"Material Mixer: {'enabled' if enabled else 'disabled'}.")
            return {'FINISHED'}

        if not _effect_bypass(mat,self.effect,enabled):
            return {'CANCELLED'}
        self.report({'INFO'},f"{self.effect}: {'enabled' if enabled else 'disabled'}.")
        return {'FINISHED'}


class ML_OT_reset_look(Operator):
    bl_idname="material_lab.reset_look"
    bl_label="Reset Effect"
    bl_options={'REGISTER','UNDO'}

    effect:StringProperty()

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat:
            return {'CANCELLED'}

        nodes=_effect_nodes(mat,self.effect)
        frame=_find_effect_frame(mat,self.effect)
        if not frame:
            return {'CANCELLED'}

        settings=mat.material_lab_settings

        if self.effect!="Material Mixer" and not bool(frame.get("ML_ENABLED",True)):
            _effect_bypass(mat,self.effect,True)

        if self.effect=="Material Mixer":
            settings.mixer_layers.clear()
            settings.mixer_collapsed=False
            _rebuild_material_mixer(mat)

        elif self.effect=="AO":
            settings.ao_amount=0.5
            settings.ao_depth=0.05
            settings.ao_distance=0.5
            settings.ao_color_blend=10.0

        elif self.effect=="Dirt":
            settings.dirt_mapping_source="GENERATED"
            settings.dirt_color=(0.12,0.035,0.008,1.0)
            settings.dirt_color_source="COLOR"
            settings.dirt_color_image=None
            settings.dirt_color_tiling=1.0
            settings.dirt_ao_amount=0.5
            settings.dirt_ao_depth=0.5
            settings.dirt_ao_distance=0.5
            settings.dirt_color_blend=1.0
            settings.dirt_amount=5.0
            settings.dirt_noise_scale=5.0
            settings.dirt_roughness=0.82

        elif self.effect=="Roughness":
            settings.roughness_source="NOISE"
            settings.roughness_image=None
            settings.roughness_tiling=1.0
            settings.roughness_mapping_source="GENERATED"
            settings.roughness_noise_scale=15.0
            settings.roughness_detail=5.0
            settings.roughness_noise_roughness=0.5
            settings.roughness_lacunarity=2.0
            settings.roughness_seed=0.0
            settings.roughness_distortion=0.0
            settings.roughness_value=0.5
            settings.roughness_noise_collapsed=False
            _update_roughness(settings,context)


        elif self.effect=="HSV":
            hsv=_find_effect_node(mat,self.effect,"HSV")
            curves=_find_effect_node(mat,self.effect,"Curves")
            if hsv:
                hsv.inputs["Hue"].default_value=0.5
                hsv.inputs["Saturation"].default_value=1.0
                hsv.inputs["Value"].default_value=1.0
                hsv.inputs["Fac"].default_value=1.0
            if curves:
                try:
                    curves.mapping.initialize()
                    curves.mapping.update()
                except Exception:
                    pass

        elif self.effect=="Grunge":
            settings.grunge_mapping_source="GENERATED"
            settings.grunge_alpha_mapping_source="GENERATED"
            settings.grunge_color_mapping_source="GENERATED"
            settings.grunge_alpha_source="NOISE"
            settings.grunge_color_source="COLOR"
            settings.grunge_alpha_image=None
            settings.grunge_color_image=None
            settings.grunge_alpha_tiling=1.0
            settings.grunge_color_tiling=1.0
            settings.grunge_noise_scale=1.3
            settings.grunge_noise_detail=15.0
            settings.grunge_noise_roughness=0.6
            settings.grunge_noise_lacunarity=2.0
            settings.grunge_noise_seed=1.0
            settings.grunge_noise_distortion=0.0
            settings.grunge_amount=5.0
            settings.grunge_blending=1.0
            settings.grunge_height=0.0
            settings.grunge_mask_contrast=0.08
            settings.grunge_noise_collapsed=False
            _update_grunge_alpha_source(settings,context)
            _update_grunge_color_source(settings,context)
            _update_grunge_mapping(settings,context)
            _update_grunge_noise(settings,context)
            color_node=_find_effect_node(mat,"Grunge","Color")
            if color_node:
                color_node.outputs["Color"].default_value=(0.12,0.035,0.008,1.0)

        for node in nodes:
            if node.get("ML_ROLE") in {"Mix","RoughnessMix","Bump","HSV"} or node.type=="HUE_SATURATION":
                node.mute=False
        frame["ML_ENABLED"]=True

        self.report({'INFO'},f"{self.effect}: values restored.")
        return {'FINISHED'}




class ML_OT_choose_source_folder(Operator):
    bl_idname="material_lab.choose_source_folder"
    bl_label="Choose Folder"
    bl_options={'REGISTER','UNDO'}
    target:StringProperty()
    directory:StringProperty(subtype='DIR_PATH')
    def invoke(self,context,event):
        context.window_manager.fileselect_add(self); return {'RUNNING_MODAL'}
    def execute(self,context):
        path=os.path.abspath(bpy.path.abspath(self.directory)) if self.directory else ""
        if not path or not os.path.isdir(path):
            self.report({'WARNING'},"The folder is not valid."); return {'CANCELLED'}
        prefs=_addon_preferences()
        if not prefs:
            self.report({'WARNING'},"Could not open Material Lab preferences.")
            return {'CANCELLED'}
        if self.target=="GRUNGE":
            prefs.grunge_source_folder=path
        else:
            prefs.texture_source_folder=path
        return {'FINISHED'}


class ML_OT_random_image(Operator):
    bl_idname="material_lab.random_image"
    bl_label="Random Image"
    bl_options={'REGISTER','UNDO'}
    effect:StringProperty(); slot:StringProperty()
    def execute(self,context):
        obj=context.active_object; mat=obj.active_material if obj else None
        if not mat: return {'CANCELLED'}
        ms=mat.material_lab_settings

        # Grunge Alpha uses the same generic random-image logic as Diffuse,
        # but targets the configured GRUNGE folder.
        if self.effect=="Grunge" and self.slot=="ALPHA":
            folder=_resource_folder("GRUNGE")
            files=_image_files_recursive(folder)
            if not files:
                self.report({'WARNING'},"No images were found in the configured Grunge folder or its subfolders."); return {'CANCELLED'}
            image=_load_image_file(random.choice(files))
            if not image:
                return {'CANCELLED'}
            ms.grunge_alpha_image=image
            ms.grunge_alpha_source="TEXTURE"
            return {'FINISHED'}

        if self.effect=="Material Mixer" and self.slot.startswith("MASK:"):
            try:
                idx=int(self.slot.split(":",1)[1])
            except Exception:
                return {'CANCELLED'}
            layers=ms.mixer_layers
            if not (0 <= idx < len(layers)):
                return {'CANCELLED'}
            files=_image_files_recursive(_resource_folder("GRUNGE"))
            if not files:
                self.report({'WARNING'},"No images were found in the Grunges folder."); return {'CANCELLED'}
            image=_load_image_file(random.choice(files))
            if not image:
                return {'CANCELLED'}
            layers[idx].mask_image=image
            if layers[idx].mask_source=="TEXTURE":
                _update_mixer_layer_nodes_in_place(mat,idx)
            else:
                layers[idx].mask_source="TEXTURE"
            return {'FINISHED'}

        if self.effect=="Roughness" and self.slot=="IMAGE":
            if ms.roughness_source=="NOISE":
                for name,lo,hi in (("roughness_noise_scale",0.0,100.0),("roughness_detail",0.0,100.0),("roughness_noise_roughness",0.0,1.0),("roughness_lacunarity",0.0,100.0),("roughness_seed",0.0,10.0),("roughness_distortion",0.0,100.0)):
                    setattr(ms,name,random.uniform(lo,hi))
                return {'FINISHED'}
            sc=context.scene.material_lab_settings
            files=_image_files_recursive(_resource_folder("TEXTURE"))
            if not files:
                self.report({'WARNING'},"No images were found in the Textures folder."); return {'CANCELLED'}
            image=_load_image_file(random.choice(files))
            if image:
                ms.roughness_image=image
                return {'FINISHED'}
            return {'CANCELLED'}

        sc=context.scene.material_lab_settings
        folder=_resource_folder("GRUNGE") if self.slot=="ALPHA" else _resource_folder("TEXTURE")
        files=_image_files_recursive(folder)
        if not files:
            self.report({'WARNING'},"No images were found in the configured folder or its subfolders."); return {'CANCELLED'}
        image=_load_image_file(random.choice(files))
        if not image: return {'CANCELLED'}
        prop={("Grunge","COLOR"):"grunge_color_image",("Dirt","COLOR"):"dirt_color_image",("Roughness","IMAGE"):"roughness_image"}.get((self.effect,self.slot))
        if not prop: return {'CANCELLED'}
        setattr(ms,prop,image)
        if self.effect=="Dirt" and self.slot=="COLOR": ms.dirt_color_source="TEXTURE"
        elif self.effect=="Roughness": ms.roughness_source="IMAGE"
        return {'FINISHED'}


class ML_OT_open_effect_image(Operator):
    bl_idname="material_lab.open_effect_image"
    bl_label="Open Image"
    bl_options={'REGISTER','UNDO'}
    effect:StringProperty(); slot:StringProperty(); filepath:StringProperty(subtype='FILE_PATH')
    def invoke(self,context,event):
        context.window_manager.fileselect_add(self); return {'RUNNING_MODAL'}
    def execute(self,context):
        image=_load_image_file(self.filepath)
        obj=context.active_object; mat=obj.active_material if obj else None
        if not image or not mat: return {'CANCELLED'}
        ms=mat.material_lab_settings
        if self.effect=="Material Mixer" and self.slot.startswith("MASK:"):
            try:
                idx=int(self.slot.split(":",1)[1])
            except Exception:
                return {'CANCELLED'}
            if not (0 <= idx < len(ms.mixer_layers)):
                return {'CANCELLED'}
            ms.mixer_layers[idx].mask_image=image
            if ms.mixer_layers[idx].mask_source=="TEXTURE":
                _update_mixer_layer_nodes_in_place(mat,idx)
            else:
                ms.mixer_layers[idx].mask_source="TEXTURE"
            return {'FINISHED'}

        prop={("Grunge","ALPHA"):"grunge_alpha_image",("Grunge","COLOR"):"grunge_color_image",("Dirt","COLOR"):"dirt_color_image",("Roughness","IMAGE"):"roughness_image"}.get((self.effect,self.slot))
        if not prop: return {'CANCELLED'}
        setattr(ms,prop,image)
        if self.effect=="Grunge" and self.slot=="ALPHA": ms.grunge_alpha_source="TEXTURE"
        elif self.effect=="Grunge" and self.slot=="COLOR": ms.grunge_color_source="TEXTURE"
        elif self.effect=="Dirt" and self.slot=="COLOR": ms.dirt_color_source="TEXTURE"
        elif self.effect=="Roughness": ms.roughness_source="IMAGE"
        return {'FINISHED'}


class ML_OT_randomize_noise(Operator):
    bl_idname="material_lab.randomize_noise"
    bl_label="Randomize Noise"
    bl_options={'REGISTER','UNDO'}
    effect:StringProperty()
    index:IntProperty(default=-1)
    def execute(self,context):
        obj=context.active_object; mat=obj.active_material if obj else None
        if not mat: return {'CANCELLED'}
        st=mat.material_lab_settings
        if self.effect=="Material Mixer" and 0 <= self.index < len(st.mixer_layers):
            layer=st.mixer_layers[self.index]
            prefix="bleed_" if layer.mask_source=="BLEED" else "mask_"
            for name,lo,hi in (
                (f"{prefix}noise_scale",0,100),(f"{prefix}noise_detail",0,100),
                (f"{prefix}noise_roughness",0,1),(f"{prefix}noise_lacunarity",0,100),
                (f"{prefix}noise_seed",0,10),(f"{prefix}noise_distortion",0,100)
            ):
                setattr(layer,name,random.uniform(lo,hi))
            return {'FINISHED'}

        groups={
            "Grunge":(
                ("grunge_noise_scale",0,100),("grunge_noise_detail",0,100),
                ("grunge_noise_roughness",0,1),("grunge_noise_lacunarity",0,100),
                ("grunge_noise_seed",0,10),("grunge_noise_distortion",0,100)
            ),
            "Roughness":(
                ("roughness_noise_scale",0,100),("roughness_detail",0,100),
                ("roughness_noise_roughness",0,1),("roughness_lacunarity",0,100),
                ("roughness_seed",0,10),("roughness_distortion",0,100)
            )
        }
        for name,lo,hi in groups.get(self.effect,()):
            prop=st.bl_rna.properties.get(name)
            if prop:
                lo=float(prop.hard_min)
                hi=float(prop.hard_max)
            setattr(st,name,random.uniform(lo,hi))
        return {'FINISHED'}



class ML_OT_reset_grunge_noise(Operator):
    bl_idname="material_lab.reset_grunge_noise"
    bl_label="Reset Grunge Noise"
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat:
            return {'CANCELLED'}
        st=mat.material_lab_settings
        st.grunge_noise_scale=1.3
        st.grunge_noise_detail=15.0
        st.grunge_noise_roughness=0.6
        st.grunge_noise_lacunarity=2.0
        st.grunge_noise_seed=1.0
        st.grunge_noise_distortion=0.0
        _update_grunge_noise(st,context)
        return {'FINISHED'}


class ML_OT_reset_grunge_alpha_mask(Operator):
    bl_idname="material_lab.reset_grunge_alpha_mask"
    bl_label="Reset Grunge Alpha Mask"
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat or not mat.node_tree:
            return {'CANCELLED'}
        ramp=mat.node_tree.nodes.get("ML_Grunge_AlphaRamp")
        if not ramp:
            return {'CANCELLED'}
        cr=ramp.color_ramp
        while len(cr.elements)>2:
            cr.elements.remove(cr.elements[-1])
        cr.elements[0].position=0.0
        cr.elements[0].color=(0.0,0.0,0.0,1.0)
        cr.elements[1].position=1.0
        cr.elements[1].color=(1.0,1.0,1.0,1.0)
        try:
            cr.interpolation='LINEAR'
        except Exception:
            pass
        mat.material_lab_settings.grunge_mask_contrast=0.08
        return {'FINISHED'}


class ML_OT_toggle_noise_collapse(Operator):
    bl_idname="material_lab.toggle_noise_collapse"
    bl_label="Toggle Noise"
    bl_options={'REGISTER','UNDO'}
    effect:StringProperty()
    def execute(self,context):
        st=context.active_object.active_material.material_lab_settings
        prop={"Grunge":"grunge_noise_collapsed","Roughness":"roughness_noise_collapsed"}.get(self.effect)
        if prop: setattr(st,prop,not getattr(st,prop))
        return {'FINISHED'}


class ML_OT_unwrap(Operator):
    bl_idname="material_lab.unwrap"
    bl_label="Desplegar UV"
    bl_options={'REGISTER','UNDO'}

    method:StringProperty()

    def execute(self,context):
        obj=context.active_object
        if not obj or obj.type!='MESH':
            self.report({'WARNING'},"Select a Mesh object.")
            return {'CANCELLED'}

        if context.mode!='EDIT_MESH':
            self.report({'WARNING'},"Enter Edit Mode to use UV.")
            return {'CANCELLED'}

        try:
            if self.method=="SMART":
                bpy.ops.uv.smart_project(angle_limit=1.15192, island_margin=0.03)
            elif self.method=="CUBE":
                bpy.ops.uv.cube_project(cube_size=1.0, correct_aspect=True)
            elif self.method=="VIEW":
                bpy.ops.uv.project_from_view(orthographic=False, camera_bounds=False, correct_aspect=True)
            elif self.method=="VIEW_BOUNDS":
                bpy.ops.uv.project_from_view(
                    orthographic=False,
                    camera_bounds=False,
                    correct_aspect=True,
                    scale_to_bounds=True
                )
            elif self.method=="UNWRAP":
                bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.03)
            elif self.method=="CYLINDER":
                bpy.ops.uv.cylinder_project(direction='VIEW_ON_EQUATOR')
            else:
                return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'},f"UV: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'},f"UV {self.method} aplicado.")
        return {'FINISHED'}


def _reset_effect_settings_to_defaults(settings,effect,context):
    if effect=="Grunge":
        settings.grunge_mapping_source="GENERATED"
        settings.grunge_alpha_mapping_source="GENERATED"
        settings.grunge_color_mapping_source="GENERATED"
        settings.grunge_alpha_source="NOISE"
        settings.grunge_color_source="COLOR"
        settings.grunge_alpha_image=None
        settings.grunge_color_image=None
        settings.grunge_alpha_tiling=1.0
        settings.grunge_color_tiling=1.0
        settings.grunge_noise_scale=1.3
        settings.grunge_noise_detail=15.0
        settings.grunge_noise_roughness=0.6
        settings.grunge_noise_lacunarity=2.0
        settings.grunge_noise_seed=1.0
        settings.grunge_noise_distortion=0.0
        settings.grunge_noise_normalize=False
        settings.grunge_amount=5.0
        settings.grunge_blending=1.0
        settings.grunge_height=0.0
        settings.grunge_mask_contrast=0.08
        settings.grunge_noise_collapsed=False
        # The effect rebuild below recreates the AlphaRamp at black=0 / white=1.
    elif effect=="AO":
        settings.ao_amount=0.5
        settings.ao_depth=0.05
        settings.ao_distance=0.5
        settings.ao_color_blend=10.0
    elif effect=="Dirt":
        settings.dirt_mapping_source="GENERATED"
        settings.dirt_color=(0.12,0.035,0.008,1.0)
        settings.dirt_color_source="COLOR"
        settings.dirt_color_image=None
        settings.dirt_color_tiling=1.0
        settings.dirt_ao_amount=0.5
        settings.dirt_ao_depth=0.5
        settings.dirt_ao_distance=0.5
        settings.dirt_color_blend=1.0
        settings.dirt_amount=5.0
        settings.dirt_noise_scale=5.0
        settings.dirt_roughness=0.82
    elif effect=="Roughness":
        settings.roughness_source="NOISE"
        settings.roughness_image=None
        settings.roughness_tiling=1.0
        settings.roughness_mapping_source="GENERATED"
        settings.roughness_noise_scale=15.0
        settings.roughness_detail=5.0
        settings.roughness_noise_roughness=0.5
        settings.roughness_lacunarity=2.0
        settings.roughness_seed=0.0
        settings.roughness_distortion=0.0
        settings.roughness_value=0.5
        settings.roughness_noise_collapsed=False
    elif effect=="HSV":
        settings.hsv_hue=0.5
        settings.hsv_saturation=1.0
        settings.hsv_value=1.0
        settings.hsv_amount=1.0
    elif effect=="Material Mixer":
        settings.mixer_layers.clear()
        settings.mixer_collapsed=False


class ML_OT_add_look(Operator):
    bl_idname="material_lab.add_look"
    bl_label="Add Effect"
    bl_options={'REGISTER','UNDO'}

    effect:StringProperty()

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if mat is None:
            self.report({'WARNING'},"The active object has no material.")
            return {'CANCELLED'}
        if _effect_nodes(mat,self.effect):
            self.report({'INFO'},f"{self.effect} is already added.")
            return {'CANCELLED'}

        # Every newly created effect starts from its own defaults.
        _reset_effect_settings_to_defaults(mat.material_lab_settings,self.effect,context)

        mat.use_nodes=True
        nodes=mat.node_tree.nodes
        links=mat.node_tree.links
        bsdf=ensure_material_node_setup(mat)
        effect=self.effect
        frame=_new_frame(mat,effect)

        # Store exactly what existed before the effect. Removal can then
        # restore the previous material state without leaving frames/links.
        _store_original_input(frame, "BASE_COLOR", bsdf.inputs["Base Color"])
        _store_original_input(frame, "ROUGHNESS", bsdf.inputs["Roughness"])
        _store_original_input(frame, "NORMAL", bsdf.inputs["Normal"])
        frame["ML_TEXTURE_SOURCE"] = "NOISE"
        frame["ML_VECTOR_SOURCE"] = "GENERATED"
        frame["ML_ENABLED"] = True

        if effect=="Material Mixer":
            frame["ML_MIXER_EFFECT"]=True
            output=next((n for n in nodes if n.type=="OUTPUT_MATERIAL"),None)
            if output:
                _store_original_surface(frame,output.inputs.get("Surface"))
            mat.material_lab_settings.mixer_layers.clear()
            _rebuild_material_mixer(mat)

        elif effect=="Grunge":
            s=mat.material_lab_settings

            # Independent coordinates: Alpha and Visual each get their own
            # Texture Coordinate + Mapping chain and their own UV/Generated/Object
            # selector. Nothing here changes the material's global coordinates.
            alpha_coord=nodes.new("ShaderNodeTexCoord")
            alpha_coord.name=_unique_node_name(mat,"ML_Grunge_AlphaCoord")
            alpha_coord.label="Grunge Alpha Coordinates"
            alpha_coord.parent=frame
            alpha_coord["ML_ROLE"]="AlphaCoord"

            alpha_mapping=nodes.new("ShaderNodeMapping")
            alpha_mapping.name=_unique_node_name(mat,"ML_Grunge_AlphaMapping")
            alpha_mapping.label="Grunge Alpha Mapping"
            alpha_mapping.parent=frame
            alpha_mapping["ML_ROLE"]="AlphaMapping"
            alpha_mapping.inputs["Scale"].default_value=(s.grunge_alpha_tiling,)*3

            color_mapping=nodes.new("ShaderNodeMapping")
            color_mapping.name=_unique_node_name(mat,"ML_Grunge_ColorMapping")
            color_mapping.label="Grunge Diffuse Mapping"
            color_mapping.parent=frame
            color_mapping["ML_ROLE"]="ColorMapping"
            color_mapping.inputs["Scale"].default_value=(s.grunge_color_tiling,)*3

            color_coord=nodes.new("ShaderNodeTexCoord")
            color_coord.name=_unique_node_name(mat,"ML_Grunge_ColorCoord")
            color_coord.label="Grunge Diffuse Coordinates"
            color_coord.parent=frame
            color_coord["ML_ROLE"]="ColorCoord"

            # Alpha sources.
            alpha_noise=nodes.new("ShaderNodeTexNoise")
            alpha_noise.name=_unique_node_name(mat,"ML_Grunge_AlphaNoise")
            alpha_noise.label="Grunge Alpha Noise"
            alpha_noise.parent=frame
            alpha_noise["ML_ROLE"]="AlphaNoise"
            alpha_noise.noise_dimensions='4D'
            alpha_noise.inputs["Scale"].default_value=s.grunge_noise_scale
            alpha_noise.inputs["Detail"].default_value=s.grunge_noise_detail
            alpha_noise.inputs["Roughness"].default_value=s.grunge_noise_roughness
            if alpha_noise.inputs.get("Lacunarity"):
                alpha_noise.inputs["Lacunarity"].default_value=s.grunge_noise_lacunarity
            if alpha_noise.inputs.get("W"):
                alpha_noise.inputs["W"].default_value=s.grunge_noise_seed
            if alpha_noise.inputs.get("Distortion"):
                alpha_noise.inputs["Distortion"].default_value=s.grunge_noise_distortion
            try:
                alpha_noise.normalize=bool(s.grunge_noise_normalize)
            except Exception:
                pass

            alpha_image=nodes.new("ShaderNodeTexImage")
            alpha_image.name=_unique_node_name(mat,"ML_Grunge_AlphaImage")
            alpha_image.label="Grunge Alpha Texture"
            alpha_image.parent=frame
            alpha_image["ML_ROLE"]="AlphaImage"

            alpha_ramp=nodes.new("ShaderNodeValToRGB")
            alpha_ramp.name=_unique_node_name(mat,"ML_Grunge_AlphaRamp")
            alpha_ramp.label="Grunge Alpha Mask"
            alpha_ramp.parent=frame
            alpha_ramp["ML_ROLE"]="AlphaRamp"
            alpha_ramp.color_ramp.elements[0].position=0.0
            alpha_ramp.color_ramp.elements[0].color=(0.0,0.0,0.0,1.0)
            alpha_ramp.color_ramp.elements[1].position=0.30
            alpha_ramp.color_ramp.elements[1].color=(1.0,1.0,1.0,1.0)

            # This Multiply is Amount: alpha mask * Amount.
            amount=nodes.new("ShaderNodeMath")
            amount.name=_unique_node_name(mat,"ML_Grunge_Amount")
            amount.label="Grunge Amount"
            amount.parent=frame
            amount["ML_ROLE"]="Amount"
            amount.operation='MULTIPLY'
            amount.inputs[1].default_value=s.grunge_amount/5.0

            # Visual source: Color or Texture.
            color=nodes.new("ShaderNodeRGB")
            color.name=_unique_node_name(mat,"ML_Grunge_Color")
            color.label="Grunge Color"
            color.parent=frame
            color["ML_ROLE"]="Color"
            color.outputs["Color"].default_value=(0.12,0.035,0.008,1.0)

            color_image=nodes.new("ShaderNodeTexImage")
            color_image.name=_unique_node_name(mat,"ML_Grunge_ColorImage")
            color_image.label="Grunge Color Texture"
            color_image.parent=frame
            color_image["ML_ROLE"]="ColorImage"

            # Base + Grunge Color: this factor is the visual opacity/blending
            # of the generated Grunge color/texture with the color underneath.
            color_mix=nodes.new("ShaderNodeMixRGB")
            color_mix.name=_unique_node_name(mat,"ML_Grunge_ColorMix")
            color_mix.label="Base + Grunge Color"
            color_mix.parent=frame
            color_mix["ML_ROLE"]="ColorMix"
            color_mix.blend_type='MIX'
            color_mix.inputs[0].default_value=s.grunge_blending

            # Final Grunge + Base: Amount controls where/how much the visual
            # result is allowed to replace the base.
            final_mix=nodes.new("ShaderNodeMixRGB")
            final_mix.name=_unique_node_name(mat,"ML_Grunge_Mix")
            final_mix.label="Grunge + Base Color"
            final_mix.parent=frame
            final_mix["ML_ROLE"]="Mix"
            final_mix["ML_FINAL"]="BASE_COLOR"
            final_mix.blend_type='MIX'

            # Signed Height: positive raises the mask, negative turns it into a cavity.
            height_invert=nodes.new("ShaderNodeMath")
            height_invert.name=_unique_node_name(mat,"ML_Grunge_HeightInvert")
            height_invert.label="Grunge Height Invert"
            height_invert.parent=frame
            height_invert["ML_ROLE"]="HeightInvert"
            height_invert.operation='SUBTRACT'
            height_invert.inputs[0].default_value=1.0

            bump=nodes.new("ShaderNodeBump")
            bump.name=_unique_node_name(mat,"ML_Grunge_Bump")
            bump.label="Grunge Height"
            bump.parent=frame
            bump["ML_ROLE"]="Bump"
            bump["ML_FINAL"]="NORMAL"
            bump.inputs["Strength"].default_value=abs(s.grunge_height)
            bump.inputs["Distance"].default_value=0.08

            base=bsdf.inputs["Base Color"]
            base_source=_capture_socket(base)
            base_value=base.default_value[:]

            # Independent mappings.
            # Both Grunge mappings start connected to Generated.
            links.new(alpha_coord.outputs["Generated"],alpha_mapping.inputs["Vector"])
            links.new(alpha_mapping.outputs["Vector"],alpha_noise.inputs["Vector"])
            links.new(alpha_mapping.outputs["Vector"],alpha_image.inputs["Vector"])
            links.new(color_coord.outputs["Generated"],color_mapping.inputs["Vector"])
            links.new(color_mapping.outputs["Vector"],color_image.inputs["Vector"])

            # Alpha source is selected by the UI updater. Default is Noise.
            links.new(alpha_noise.outputs["Fac"],alpha_ramp.inputs["Fac"])
            links.new(alpha_ramp.outputs["Color"],amount.inputs[0])

            # Amount controls final mix factor.
            links.new(amount.outputs["Value"],final_mix.inputs[0])

            links.new(alpha_ramp.outputs["Color"],height_invert.inputs[1])
            if s.grunge_height < 0.0:
                links.new(height_invert.outputs["Value"],bump.inputs["Height"])
            else:
                links.new(alpha_ramp.outputs["Color"],bump.inputs["Height"])

            # Preserve the base color source.
            if base_source:
                links.new(base_source,color_mix.inputs[1])
                links.new(base_source,final_mix.inputs[1])
            else:
                color_mix.inputs[1].default_value=base_value
                final_mix.inputs[1].default_value=base_value

            # Default visual source is Color.
            links.new(color.outputs["Color"],color_mix.inputs[2])

            # ColorMix factor = visual Blending.
            links.new(color_mix.outputs["Color"],final_mix.inputs[2])

            for link in list(base.links):
                links.remove(link)
            links.new(final_mix.outputs["Color"],base)

            for link in list(bsdf.inputs["Normal"].links):
                links.remove(link)
            links.new(bump.outputs["Normal"],bsdf.inputs["Normal"])

            frame["ML_COLOR_ENABLED"]=True
            frame["ML_ALPHA_SOURCE"]=s.grunge_alpha_source
            frame["ML_COLOR_SOURCE"]=s.grunge_color_source
            frame["ML_ALPHA_MAPPING_SOURCE"]=s.grunge_alpha_mapping_source
            frame["ML_COLOR_MAPPING_SOURCE"]=s.grunge_color_mapping_source

            _update_grunge_mapping(s,context)
            _update_grunge_alpha_source(s,context)
            _update_grunge_color_source(s,context)
            _update_grunge_noise(s,context)

        elif effect=="AO":
            base=bsdf.inputs["Base Color"]
            base_source=_capture_socket(base)
            base_value=base.default_value[:]

            ao=nodes.new("ShaderNodeAmbientOcclusion")
            ao.name=_unique_node_name(mat,"ML_AO_Texture")
            ao.label="Ambient Occlusion"
            ao.parent=frame
            ao["ML_ROLE"]="AO"
            ao.inputs["Distance"].default_value=mat.material_lab_settings.dirt_ao_distance
            if ao.inputs.get("Samples"):
                ao.inputs["Samples"].default_value=10

            ramp=nodes.new("ShaderNodeValToRGB")
            ramp.name=_unique_node_name(mat,"ML_AO_Ramp")
            ramp.label="AO Color Ramp"
            ramp.parent=frame
            ramp["ML_ROLE"]="Ramp"
            ramp["ML_AO_MASK"]=True
            ramp.color_ramp.elements[0].position=0.05
            ramp.color_ramp.elements[0].color=(1,1,1,1)
            ramp.color_ramp.elements[1].position=0.20
            ramp.color_ramp.elements[1].color=(0,0,0,1)

            color_mix=nodes.new("ShaderNodeMixRGB")
            color_mix.name=_unique_node_name(mat,"ML_AO_ColorMix")
            color_mix.label="AO Color Blend"
            color_mix.parent=frame
            color_mix["ML_ROLE"]="ColorMix"
            color_mix.blend_type='MIX'
            color_mix.inputs[0].default_value=0.5

            mix=nodes.new("ShaderNodeMixRGB")
            mix.name=_unique_node_name(mat,"ML_AO_Mix")
            mix.label="AO Cavity Mix"
            mix.parent=frame
            mix["ML_ROLE"]="Mix"
            mix["ML_FINAL"]="BASE_COLOR"
            mix.blend_type='MIX'

            color=nodes.new("ShaderNodeRGB")
            color.name=_unique_node_name(mat,"ML_AO_Color")
            color.label="AO Color"
            color.parent=frame
            color["ML_ROLE"]="Color"
            color.outputs["Color"].default_value=(0.05,0.05,0.05,1)

            links.new(ao.outputs["AO"],ramp.inputs["Fac"])
            links.new(ramp.outputs["Color"],mix.inputs[0])
            links.new(color.outputs["Color"],color_mix.inputs[2])

            # Color Blend controls only how strongly the AO color replaces
            # the original base color. Outside the AO mask the original
            # material remains untouched.
            if base_source:
                links.new(base_source,color_mix.inputs[1])
            else:
                color_mix.inputs[1].default_value=base_value

            links.new(base_source,mix.inputs[1]) if base_source else None
            links.new(color_mix.outputs["Color"],mix.inputs[2])

            if not base_source:
                mix.inputs[1].default_value=base_value

            for link in list(base.links):
                links.remove(link)
            links.new(mix.outputs["Color"],base)

        
        elif effect=="Dirt":
            # Dirt = brown color + extra roughness, masked by AO and broken
            # with procedural noise. Optional image texture can be used as
            # an additional dirt mask.
            base=bsdf.inputs["Base Color"]
            base_source=_capture_socket(base)
            base_value=base.default_value[:]

            rough=bsdf.inputs["Roughness"]
            rough_source=_capture_socket(rough)
            rough_value=rough.default_value

            texcoord=nodes.new("ShaderNodeTexCoord")
            texcoord.name=_unique_node_name(mat,"ML_Dirt_Coord")
            texcoord.label="Dirt Coordinates"
            texcoord.parent=frame
            texcoord["ML_ROLE"]="Coord"

            mapping=nodes.new("ShaderNodeMapping")
            mapping.name=_unique_node_name(mat,"ML_Dirt_Mapping")
            mapping.label="Dirt Mapping"
            mapping.parent=frame
            mapping["ML_ROLE"]="Mapping"

            color_mapping=nodes.new("ShaderNodeMapping")
            color_mapping.name=_unique_node_name(mat,"ML_Dirt_ColorMapping")
            color_mapping.label="Dirt Color Mapping"
            color_mapping.parent=frame
            color_mapping["ML_ROLE"]="ColorMapping"
            color_mapping.inputs["Scale"].default_value=(1.0,1.0,1.0)

            color_image=nodes.new("ShaderNodeTexImage")
            color_image.name=_unique_node_name(mat,"ML_Dirt_ColorImage")
            color_image.label="Dirt Color Texture"
            color_image.parent=frame
            color_image["ML_ROLE"]="ColorImage"
            color_image.image=mat.material_lab_settings.dirt_color_image

            noise=nodes.new("ShaderNodeTexNoise")
            noise.name=_unique_node_name(mat,"ML_Dirt_Noise")
            noise.label="Dirt Noise"
            noise.parent=frame
            noise["ML_ROLE"]="Texture"
            noise.noise_dimensions='4D'
            noise.inputs["Scale"].default_value=5.0
            noise.inputs["Detail"].default_value=5.0
            noise.inputs["Roughness"].default_value=0.75
            noise.inputs["W"].default_value=0.0

            ao=nodes.new("ShaderNodeAmbientOcclusion")
            ao.name=_unique_node_name(mat,"ML_Dirt_AO")
            ao.label="Dirt AO"
            ao.parent=frame
            ao["ML_ROLE"]="AO"
            ao.inputs["Distance"].default_value=mat.material_lab_settings.dirt_ao_distance
            if ao.inputs.get("Samples"):
                ao.inputs["Samples"].default_value=8

            ao_ramp=nodes.new("ShaderNodeValToRGB")
            ao_ramp.name=_unique_node_name(mat,"ML_Dirt_AO_Ramp")
            ao_ramp.label="AO Mask"
            ao_ramp.parent=frame
            ao_ramp["ML_ROLE"]="AORamp"
            ao_ramp.color_ramp.elements[0].position=0.35
            ao_ramp.color_ramp.elements[1].position=0.70

            noise_ramp=nodes.new("ShaderNodeValToRGB")
            noise_ramp.name=_unique_node_name(mat,"ML_Dirt_Noise_Ramp")
            noise_ramp.label="Dirt Breakup"
            noise_ramp.parent=frame
            noise_ramp["ML_ROLE"]="Ramp"
            noise_ramp.color_ramp.elements[0].position=0.30
            noise_ramp.color_ramp.elements[1].position=0.68

            mask_mix=nodes.new("ShaderNodeMixRGB")
            mask_mix.name=_unique_node_name(mat,"ML_Dirt_Mask_Mix")
            mask_mix.label="AO × Noise"
            mask_mix.parent=frame
            mask_mix["ML_ROLE"]="Mask"
            mask_mix.blend_type='MULTIPLY'
            mask_mix.inputs[0].default_value=1.0

            mask_ramp=nodes.new("ShaderNodeValToRGB")
            mask_ramp.name=_unique_node_name(mat,"ML_Dirt_Mask_Ramp")
            mask_ramp.label="Dirt Mask Contrast"
            mask_ramp.parent=frame
            mask_ramp["ML_ROLE"]="MaskRamp"
            mask_ramp.color_ramp.elements[0].position=0.0
            mask_ramp.color_ramp.elements[0].color=(0,0,0,1)
            mask_ramp.color_ramp.elements[1].position=0.25
            mask_ramp.color_ramp.elements[1].color=(1,1,1,1)

            dirt_color=nodes.new("ShaderNodeRGB")
            dirt_color.name=_unique_node_name(mat,"ML_Dirt_Color")
            dirt_color.label="Dirt Color"
            dirt_color.parent=frame
            dirt_color["ML_ROLE"]="Color"
            dirt_color.outputs["Color"].default_value=mat.material_lab_settings.dirt_color

            color_blend=nodes.new("ShaderNodeMixRGB")
            color_blend.name=_unique_node_name(mat,"ML_Dirt_ColorBlend")
            color_blend.label="Dirt Color Blend"
            color_blend.parent=frame
            color_blend["ML_ROLE"]="ColorBlend"
            color_blend.blend_type='MIX'
            color_blend.inputs[0].default_value=max(0.0,min(1.0,float(mat.material_lab_settings.dirt_color_blend)))
            if base_source:
                links.new(base_source,color_blend.inputs[1])
            else:
                color_blend.inputs[1].default_value=base_value

            amount_color=nodes.new("ShaderNodeMath")
            amount_color.name=_unique_node_name(mat,"ML_Dirt_Amount_Color")
            amount_color.label="Dirt Intensity"
            amount_color.parent=frame
            amount_color["ML_ROLE"]="Amount"
            amount_color.operation='MULTIPLY'
            amount_color.inputs[1].default_value=1.0

            amount_rough=nodes.new("ShaderNodeMath")
            amount_rough.name=_unique_node_name(mat,"ML_Dirt_Amount_Roughness")
            amount_rough.label="Dirt Roughness Amount"
            amount_rough.parent=frame
            amount_rough["ML_ROLE"]="AmountRoughness"
            amount_rough.operation='MULTIPLY'
            amount_rough.inputs[1].default_value=1.0

            color_mix=nodes.new("ShaderNodeMixRGB")
            color_mix.name=_unique_node_name(mat,"ML_Dirt_Color_Mix")
            color_mix.label="Brown Dirt"
            color_mix.parent=frame
            color_mix["ML_ROLE"]="Mix"
            color_mix["ML_FINAL"]="BASE_COLOR"
            color_mix.blend_type='MIX'
            color_mix.inputs[0].default_value=1.0

            rough_mix=nodes.new("ShaderNodeMixRGB")
            rough_mix.name=_unique_node_name(mat,"ML_Dirt_Roughness_Mix")
            rough_mix.label="Dirt Roughness"
            rough_mix.parent=frame
            rough_mix["ML_ROLE"]="RoughnessMix"
            rough_mix["ML_FINAL"]="ROUGHNESS"
            rough_mix.blend_type='MIX'
            rough_mix.inputs[0].default_value=1.0
            rough_mix.inputs[2].default_value=(mat.material_lab_settings.dirt_roughness,mat.material_lab_settings.dirt_roughness,mat.material_lab_settings.dirt_roughness,1)

            links.new(texcoord.outputs["Generated"],mapping.inputs["Vector"])
            links.new(mapping.outputs["Vector"],noise.inputs["Vector"])

            links.new(noise.outputs["Fac"],noise_ramp.inputs["Fac"])

            # AO is inverted into a cavity mask: white at strong occlusion,
            # then the black threshold grows outward as Amount increases.
            links.new(ao.outputs["AO"],ao_ramp.inputs["Fac"])
            ao_ramp.color_ramp.elements[0].position=0.0
            ao_ramp.color_ramp.elements[0].color=(1,1,1,1)
            ao_ramp.color_ramp.elements[1].position=0.45
            ao_ramp.color_ramp.elements[1].color=(0,0,0,1)

            links.new(ao_ramp.outputs["Color"],mask_mix.inputs[1])
            links.new(noise_ramp.outputs["Color"],mask_mix.inputs[2])
            links.new(mask_mix.outputs["Color"],mask_ramp.inputs["Fac"])

            # Amount multiplies the cavity/breakup mask, so the Dirt Amount
            # slider now has a real effect on both color and roughness.
            links.new(mask_ramp.outputs["Color"],amount_color.inputs[0])
            links.new(mask_ramp.outputs["Color"],amount_rough.inputs[0])
            links.new(amount_color.outputs["Value"],color_mix.inputs[0])
            links.new(amount_rough.outputs["Value"],rough_mix.inputs[0])
            links.new(dirt_color.outputs["Color"],color_blend.inputs[2])
            links.new(color_blend.outputs["Color"],color_mix.inputs[2])
            links.new(texcoord.outputs["Generated"],color_mapping.inputs["Vector"])
            links.new(color_mapping.outputs["Vector"],color_image.inputs["Vector"])

            if base_source:
                links.new(base_source,color_mix.inputs[1])
            else:
                color_mix.inputs[1].default_value=base_value

            if rough_source:
                links.new(rough_source,rough_mix.inputs[1])
            else:
                rough_mix.inputs[1].default_value=(rough_value, rough_value, rough_value, 1.0)

            for l in list(base.links): links.remove(l)
            for l in list(rough.links): links.remove(l)
            links.new(color_mix.outputs["Color"],base)
            links.new(rough_mix.outputs["Color"],rough)

            _set_effect_mapping_source(mat,"Dirt",mat.material_lab_settings.dirt_mapping_source)
            _update_dirt_ao_amount(mat.material_lab_settings, None)
            _update_dirt_color_source(mat.material_lab_settings, None)
            _update_dirt_color_image(mat.material_lab_settings, None)

        elif effect=="Roughness":
            rough=bsdf.inputs["Roughness"]
            rough_source=_capture_socket(rough)
            rough_value=float(rough.default_value)

            coord=nodes.new("ShaderNodeTexCoord")
            coord.name=_unique_node_name(mat,"ML_Roughness_Coord")
            coord.parent=frame; coord["ML_ROLE"]="Coord"

            mapping=nodes.new("ShaderNodeMapping")
            mapping.name=_unique_node_name(mat,"ML_Roughness_Mapping")
            mapping.parent=frame; mapping["ML_ROLE"]="Mapping"
            mapping.inputs["Scale"].default_value=(1.0,1.0,1.0)

            noise=nodes.new("ShaderNodeTexNoise")
            noise.name=_unique_node_name(mat,"ML_Roughness_Noise")
            noise.label="Roughness Noise"; noise.parent=frame; noise["ML_ROLE"]="Noise"
            noise.noise_dimensions='4D'
            st=mat.material_lab_settings
            for name,value in (("Scale",st.roughness_noise_scale),("Detail",st.roughness_detail),("Roughness",st.roughness_noise_roughness),("Lacunarity",st.roughness_lacunarity),("W",st.roughness_seed),("Distortion",st.roughness_distortion)):
                if noise.inputs.get(name): noise.inputs[name].default_value=float(value)

            image=nodes.new("ShaderNodeTexImage")
            image.name=_unique_node_name(mat,"ML_Roughness_Image")
            image.parent=frame; image["ML_ROLE"]="Image"; image.image=st.roughness_image

            mask=nodes.new("ShaderNodeValToRGB")
            mask.name=_unique_node_name(mat,"ML_Roughness_Mask")
            mask.label="Roughness Mask"; mask.parent=frame; mask["ML_ROLE"]="Mask"
            mask.color_ramp.elements[0].position=0.0; mask.color_ramp.elements[0].color=(0,0,0,1)
            mask.color_ramp.elements[1].position=1.0; mask.color_ramp.elements[1].color=(1,1,1,1)

            amount=nodes.new("ShaderNodeMath")
            amount.name=_unique_node_name(mat,"ML_Roughness_Amount")
            amount.label="Roughness Value"; amount.parent=frame; amount["ML_ROLE"]="Amount"
            amount.operation='MULTIPLY'; amount.inputs[1].default_value=st.roughness_value

            mix=nodes.new("ShaderNodeMixRGB")
            mix.name=_unique_node_name(mat,"ML_Roughness_Mix")
            mix.label="Roughness Layer"; mix.parent=frame; mix["ML_ROLE"]="Mix"; mix["ML_FINAL"]="ROUGHNESS"
            mix.blend_type='MIX'

            links.new(coord.outputs["Generated"],mapping.inputs["Vector"])
            links.new(mapping.outputs["Vector"],noise.inputs["Vector"])
            links.new(mapping.outputs["Vector"],image.inputs["Vector"])
            if st.roughness_source=="IMAGE" and image.image:
                links.new(image.outputs["Color"],mask.inputs["Fac"])
            else:
                links.new(noise.outputs["Fac"],mask.inputs["Fac"])
            links.new(mask.outputs["Color"],mix.inputs[0])
            links.new(mask.outputs["Color"],amount.inputs[0])
            if rough_source: links.new(rough_source,mix.inputs[1])
            else: mix.inputs[1].default_value=(rough_value,rough_value,rough_value,1.0)
            links.new(amount.outputs["Value"],mix.inputs[2])
            for link in list(rough.links): links.remove(link)
            links.new(mix.outputs["Color"],rough)
            _set_effect_mapping_source(mat,"Roughness",st.roughness_mapping_source)

        elif effect=="HSV":
            base=bsdf.inputs["Base Color"]
            base_source=_capture_socket(base)
            base_value=base.default_value[:]

            hsv=nodes.new("ShaderNodeHueSaturation")
            hsv.name=_unique_node_name(mat,"ML_HSV")
            hsv.label="Hue / Saturation / Value"
            hsv.parent=frame
            hsv["ML_ROLE"]="HSV"
            hsv["ML_EFFECT"]="HSV"
            hsv["ML_FINAL"]="BASE_COLOR"
            hsv.inputs["Hue"].default_value=mat.material_lab_settings.hsv_hue
            hsv.inputs["Saturation"].default_value=mat.material_lab_settings.hsv_saturation
            hsv.inputs["Value"].default_value=mat.material_lab_settings.hsv_value
            hsv.inputs["Fac"].default_value=mat.material_lab_settings.hsv_amount

            curves=nodes.new("ShaderNodeRGBCurve")
            curves.name=_unique_node_name(mat,"ML_Color_Curves")
            curves.label="Color Curves"
            curves.parent=frame
            curves["ML_ROLE"]="Curves"
            curves["ML_EFFECT"]="HSV"
            curves["ML_FINAL"]="BASE_COLOR"

            if base_source:
                links.new(base_source,hsv.inputs["Color"])
            else:
                hsv.inputs["Color"].default_value=base_value
            links.new(hsv.outputs["Color"],curves.inputs["Color"])
            for l in list(base.links): links.remove(l)
            links.new(curves.outputs["Color"],base)
            frame.label="Color"

        else:
            nodes.remove(frame)
            return {'CANCELLED'}

        # Adding an effect must never change the Base Material mapping.
        # Base mapping is controlled only from Object Materials > Mapping.
        _layout_new_effect(mat, effect)
        self.report({'INFO'},f"{effect} agregado.")
        return {'FINISHED'}


class ML_OT_remove_look(Operator):
    bl_idname="material_lab.remove_look"
    bl_label="Delete Effect"
    bl_options={'REGISTER','UNDO'}
    effect:StringProperty()

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat:
            return {'CANCELLED'}
        if self.effect=="Material Mixer":
            frame=_find_effect_frame(mat,"Material Mixer")
            if not frame:
                return {'CANCELLED'}
            nodes=mat.node_tree.nodes; links=mat.node_tree.links
            output=next((n for n in nodes if n.type=="OUTPUT_MATERIAL"),None)
            if output:
                surface=output.inputs.get("Surface")
                for l in list(surface.links): links.remove(l)
                n=nodes.get(frame.get("ML_ORIG_SURFACE_NODE",""))
                s=n.outputs.get(frame.get("ML_ORIG_SURFACE_SOCKET","")) if n else None
                if s: links.new(s,surface)
            # Remove generated nodes and every copied node inside the mixer
            # frame; copied source nodes are explicitly tagged as generated.
            generated=[n for n in list(nodes) if n.get("ML_MIXER_GENERATED")]
            for n in generated:
                if n.name in nodes:
                    nodes.remove(n)
            if frame.name in nodes:
                nodes.remove(frame)
            mat.material_lab_settings.mixer_layers.clear()
            self.report({'INFO'},"Material Mixer deleted.")
            return {'FINISHED'}
        if _remove_effect_nodes(mat,self.effect):
            self.report({'INFO'},f"{self.effect} deleted.")
            return {'FINISHED'}
        return {'CANCELLED'}





def _copy_node_tree_into_frame(src_mat, dst_mat, frame, prefix="ML_MIX_SRC"):
    """Copy a material shader tree into a mixer frame with normalized local coordinates."""
    if not src_mat or not src_mat.node_tree:
        return None

    src_nodes=src_mat.node_tree.nodes
    src_links=src_mat.node_tree.links
    dst_nodes=dst_mat.node_tree.nodes
    dst_links=dst_mat.node_tree.links
    node_map={}

    source_nodes=[n for n in src_nodes if n.type!="OUTPUT_MATERIAL"]
    if not source_nodes:
        return None

    min_x=min(float(n.location.x) for n in source_nodes)
    max_y=max(float(n.location.y) for n in source_nodes)

    # Keep a readable, compact copy regardless of the source material's
    # original node-editor zoom/position.
    for src in source_nodes:
        try:
            new=dst_nodes.new(src.bl_idname)
        except Exception:
            continue
        new.name=_unique_node_name(dst_mat,f"{prefix}_{src.name}")
        new.label=src.label or src.name
        new.parent=frame
        new.location=(float(src.location.x)-min_x, float(src.location.y)-max_y)
        new["ML_MIXER_GENERATED"]=True
        new["ML_MIXER_SOURCE_NODE"]=src.name
        try: new.width=src.width
        except Exception: pass
        try: new.hide=src.hide
        except Exception: pass

        if hasattr(src,"image") and hasattr(new,"image"):
            try: new.image=src.image
            except Exception: pass
        if src.type=="GROUP" and hasattr(src,"node_tree") and hasattr(new,"node_tree"):
            try: new.node_tree=src.node_tree
            except Exception: pass
        if src.type=="RGB" and src.outputs.get("Color") and new.outputs.get("Color"):
            try: new.outputs["Color"].default_value=src.outputs["Color"].default_value
            except Exception: pass

        for si,di in zip(src.inputs,new.inputs):
            if si.is_linked:
                continue
            try:
                di.default_value=si.default_value
            except Exception:
                pass
        node_map[src]=new

    for link in src_links:
        if link.from_node.type=="OUTPUT_MATERIAL":
            continue
        fn=node_map.get(link.from_node)
        tn=node_map.get(link.to_node)
        if not fn or not tn:
            continue
        try:
            fo=fn.outputs.get(link.from_socket.name)
            ti=tn.inputs.get(link.to_socket.name)
            if fo and ti:
                dst_links.new(fo,ti)
        except Exception:
            try:
                fo=fn.outputs[link.from_socket.index]
                ti=tn.inputs[link.to_socket.index]
                if fo and ti:
                    dst_links.new(fo,ti)
            except Exception:
                pass

    src_output=next((n for n in src_nodes if n.type=="OUTPUT_MATERIAL"),None)
    if not src_output:
        return None
    surf=src_output.inputs.get("Surface")
    if not surf or not surf.is_linked:
        return None
    src_shader=surf.links[0].from_node
    src_socket=surf.links[0].from_socket
    copied_node=node_map.get(src_shader)
    if not copied_node:
        return None
    return copied_node.outputs.get(src_socket.name) or (
        copied_node.outputs[src_socket.index]
        if src_socket.index < len(copied_node.outputs) else None
    )




def _layout_mixer_source_nodes(frame):
    """Spread copied shader nodes inside a Material Mixer layer.
    This changes positions only; it does not alter the mixer graph.
    """
    if not frame or not frame.id_data:
        return
    nodes=frame.id_data.nodes
    children=[
        n for n in nodes
        if n.parent==frame and n.get("ML_MIXER_GENERATED") and n.type!="FRAME"
    ]
    if not children:
        return

    min_x=min(float(n.location.x) for n in children)
    max_y=max(float(n.location.y) for n in children)
    for n in children:
        n.location.x=(float(n.location.x)-min_x)*1.45
        n.location.y=(float(n.location.y)-max_y)*1.35

    gap_x=80.0
    gap_y=45.0
    for _ in range(40):
        moved=False
        ordered=sorted(children,key=lambda n:(float(n.location.y),float(n.location.x)))
        for i,a in enumerate(ordered):
            ax=float(a.location.x)
            ay=float(a.location.y)
            aw=max(float(getattr(a.dimensions,"x",0.0)),float(getattr(a,"width",140.0)))
            ah=max(float(getattr(a.dimensions,"y",0.0)),60.0)
            for b in ordered[i+1:]:
                bx=float(b.location.x)
                by=float(b.location.y)
                bw=max(float(getattr(b.dimensions,"x",0.0)),float(getattr(b,"width",140.0)))
                bh=max(float(getattr(b.dimensions,"y",0.0)),60.0)
                overlap_x=(ax+aw+gap_x)-bx
                overlap_y=(ay+ah+gap_y)-by
                if overlap_x>0.0 and overlap_y>0.0:
                    b.location.x += overlap_x
                    moved=True
        if not moved:
            break


def _mixer_ramp_positions(ramp_node, default_black=0.0, default_white=1.0):
    if not ramp_node:
        return default_black, default_white
    try:
        cr=ramp_node.color_ramp
        return float(cr.elements[0].position), float(cr.elements[-1].position)
    except Exception:
        return default_black, default_white


def _capture_mixer_ramp_state(mat):
    """Capture current node ramp handle positions before a structural rebuild."""
    if not mat or not mat.node_tree:
        return
    settings=mat.material_lab_settings
    nodes=mat.node_tree.nodes
    for idx,layer in enumerate(settings.mixer_layers):
        ramp=next((n for n in nodes if n.get("ML_MIXER_MASK_RAMP_INDEX")==idx),None)
        if ramp:
            layer.mask_ramp_black,layer.mask_ramp_white=_mixer_ramp_positions(
                ramp,layer.mask_ramp_black,layer.mask_ramp_white
            )

        bleed_ramp=next((n for n in nodes if n.get("ML_MIXER_BLEED_RAMP_INDEX")==idx),None)
        if bleed_ramp:
            layer.bleed_ramp_black,layer.bleed_ramp_white=_mixer_ramp_positions(
                bleed_ramp,layer.bleed_ramp_black,layer.bleed_ramp_white
            )

        bleed_noise_ramp=next((n for n in nodes if n.get("ML_MIXER_BLEED_NOISE_RAMP_INDEX")==idx),None)
        if bleed_noise_ramp:
            layer.bleed_noise_ramp_black,layer.bleed_noise_ramp_white=_mixer_ramp_positions(
                bleed_noise_ramp,layer.bleed_noise_ramp_black,layer.bleed_noise_ramp_white
            )


def _set_mixer_ramp(cr, black_pos, white_pos):
    try:
        cr.elements[0].position=max(0.0,min(1.0,float(min(black_pos,white_pos-0.001))))
        cr.elements[-1].position=max(cr.elements[0].position+0.001,min(1.0,float(max(white_pos,cr.elements[0].position+0.001))))
    except Exception:
        pass


def _rebuild_material_mixer(mat):
    if not mat or not mat.node_tree:
        return False
    _capture_mixer_ramp_state(mat)
    frame=_find_effect_frame(mat,"Material Mixer")
    if not frame:
        return False

    nodes=mat.node_tree.nodes
    links=mat.node_tree.links
    saved_positions={}
    for n in nodes:
        if not n.get("ML_MIXER_GENERATED"):
            continue
        for prop in (
            "ML_MIXER_SOURCE_INDEX",
            "ML_MIXER_MASK_COORD_INDEX",
            "ML_MIXER_MASK_MAPPING_INDEX",
            "ML_MIXER_MASK_NOISE_INDEX",
            "ML_MIXER_MASK_IMAGE_INDEX",
            "ML_MIXER_MASK_RAMP_INDEX",
            "ML_MIXER_BLEED_COORD_INDEX",
            "ML_MIXER_BLEED_MAPPING_INDEX",
            "ML_MIXER_BLEED_GRADIENT_INDEX",
            "ML_MIXER_BLEED_RAMP_INDEX",
            "ML_MIXER_BLEED_NOISE_MAPPING_INDEX",
            "ML_MIXER_BLEED_NOISE_INDEX",
            "ML_MIXER_BLEED_NOISE_RAMP_INDEX",
            "ML_MIXER_BLEED_BREAK_MULTIPLY_INDEX",
            "ML_MIXER_BLEED_BREAK_SUBTRACT_INDEX",
            "ML_MIXER_FACTOR_INDEX",
            "ML_MIXER_HEIGHT_INVERT_INDEX",
            "ML_MIXER_HEIGHT_BUMP_INDEX",
            "ML_MIXER_MIX_INDEX",
        ):
            if prop in n:
                saved_positions[(prop,int(n.get(prop)))]=(float(n.location.x),float(n.location.y))
                break
    output=next((n for n in nodes if n.type=="OUTPUT_MATERIAL"),None)
    if not output:
        output=nodes.new("ShaderNodeOutputMaterial")
    surface=output.inputs.get("Surface")

    # The base is always the surface that existed before the Mixer effect.
    base_socket=None
    orig_node_name=frame.get("ML_ORIG_SURFACE_NODE","")
    orig_socket_name=frame.get("ML_ORIG_SURFACE_SOCKET","")
    if orig_node_name:
        orig_node=nodes.get(orig_node_name)
        if orig_node:
            base_socket=orig_node.outputs.get(orig_socket_name)

    if not base_socket and surface and surface.is_linked:
        candidate=surface.links[0].from_socket
        if not candidate.node.get("ML_MIXER_GENERATED"):
            base_socket=candidate

    for n in list(nodes):
        if n.get("ML_MIXER_GENERATED"):
            nodes.remove(n)

    if not base_socket:
        bsdf=ensure_material_node_setup(mat)
        base_socket=bsdf.outputs.get("BSDF")
    if not base_socket:
        return False

    layers=mat.material_lab_settings.mixer_layers
    current=base_socket

    for idx,layer in enumerate(layers):
        if not layer.material or layer.material==mat:
            continue

        y=-idx*760
        source_frame=nodes.new("NodeFrame")
        source_frame.name=_unique_node_name(mat,f"ML_MaterialMixer_Source_{idx+2}")
        source_frame.label=f"Material {idx+2}: {layer.material.name}"
        source_frame.parent=frame
        source_frame.location=(180,y)
        source_frame["ML_MIXER_GENERATED"]=True
        source_frame["ML_MIXER_SOURCE_INDEX"]=idx

        copied=_copy_node_tree_into_frame(
            layer.material,mat,source_frame,f"ML_MIXER_{idx+2}"
        )
        if not copied:
            nodes.remove(source_frame)
            continue

        _layout_mixer_source_nodes(source_frame)

        factor_socket=None
        if layer.mask_source!="NONE":
            if layer.mask_source in {"TEXTURE","NOISE"}:
                coord=nodes.new("ShaderNodeTexCoord")
                coord.name=_unique_node_name(mat,f"ML_MaterialMixer_MaskCoord_{idx}")
                coord.label="Mixer Mask Coordinates"
                coord.parent=frame
                coord.location=(-1150,y-380)
                coord["ML_MIXER_GENERATED"]=True
                coord["ML_MIXER_MASK_COORD_INDEX"]=idx
                try:
                    coord.object=layer.mask_object if layer.mask_object and layer.mask_object.type=="EMPTY" else None
                except Exception:
                    pass

                mapping=nodes.new("ShaderNodeMapping")
                mapping.name=_unique_node_name(mat,f"ML_MaterialMixer_MaskMapping_{idx}")
                mapping.label="Mixer Mask Mapping"
                mapping.parent=frame
                mapping.location=(-900,y-380)
                mapping["ML_MIXER_GENERATED"]=True
                mapping["ML_MIXER_MASK_MAPPING_INDEX"]=idx
                mapping.inputs["Scale"].default_value=(float(layer.mask_tiling),)*3

                socket=coord.outputs.get(layer.mask_mapping_source)
                if socket:
                    links.new(socket,mapping.inputs["Vector"])

                if layer.mask_source=="TEXTURE":
                    image=nodes.new("ShaderNodeTexImage")
                    image.name=_unique_node_name(mat,f"ML_MaterialMixer_MaskImage_{idx}")
                    image.label="Mixer Mask Texture"
                    image.parent=frame
                    image.location=(-620,y-380)
                    image.image=layer.mask_image
                    image["ML_MIXER_GENERATED"]=True
                    image["ML_MIXER_MASK_IMAGE_INDEX"]=idx
                    links.new(mapping.outputs["Vector"],image.inputs["Vector"])
                    source=image.outputs.get("Color")

                    # The painted image is the mask directly.
                    source=image.outputs.get("Color")
                else:
                    noise=nodes.new("ShaderNodeTexNoise")
                    noise.name=_unique_node_name(mat,f"ML_MaterialMixer_MaskNoise_{idx}")
                    noise.label="Mixer Mask Noise"
                    noise.parent=frame
                    noise.location=(-620,y-380)
                    noise["ML_MIXER_GENERATED"]=True
                    noise["ML_MIXER_MASK_NOISE_INDEX"]=idx
                    noise.noise_dimensions='4D'
                    for name,value in (
                        ("Scale",layer.mask_noise_scale),
                        ("Detail",layer.mask_noise_detail),
                        ("Roughness",layer.mask_noise_roughness),
                        ("Lacunarity",layer.mask_noise_lacunarity),
                        ("W",layer.mask_noise_seed),
                        ("Distortion",layer.mask_noise_distortion),
                    ):
                        if noise.inputs.get(name):
                            noise.inputs[name].default_value=float(value)
                    try:
                        noise.normalize=bool(layer.mask_noise_normalize)
                    except Exception:
                        pass
                    links.new(mapping.outputs["Vector"],noise.inputs["Vector"])
                    source=noise.outputs.get("Fac")

                ramp=nodes.new("ShaderNodeValToRGB")
                ramp.name=_unique_node_name(mat,f"ML_MaterialMixer_MaskRamp_{idx}")
                ramp.label="Mixer Mask Contrast"
                ramp.parent=frame
                ramp.location=(-360,y-380)
                ramp["ML_MIXER_GENERATED"]=True
                ramp["ML_MIXER_MASK_RAMP_INDEX"]=idx
                _set_mixer_ramp(ramp.color_ramp,layer.mask_ramp_black,layer.mask_ramp_white)
                links.new(source,ramp.inputs["Fac"])
                source=ramp.outputs["Color"]

                factor=nodes.new("ShaderNodeMath")
                factor.name=_unique_node_name(mat,f"ML_MaterialMixer_MaskFactor_{idx}")
                factor.label="Blend × Mask"
                factor.parent=frame
                factor.location=(-120,y-380)
                factor.operation='MULTIPLY'
                factor.inputs[1].default_value=(
                    0.0 if not layer.enabled
                    else (1.0 if layer.blend_mode=="REPLACE" else float(layer.amount))
                )
                factor["ML_MIXER_GENERATED"]=True
                factor["ML_MIXER_FACTOR_INDEX"]=idx
                links.new(source,factor.inputs[0])
                factor_socket=factor.outputs["Value"]

            elif layer.mask_source=="BLEED":
                # Target controls a clean vertical gradient. Break Border subtracts
                # noise from that gradient, so Break Border=0 is perfectly clean
                # and increasing it makes the noise descend from the boundary.
                coord=nodes.new("ShaderNodeTexCoord")
                coord.name=_unique_node_name(mat,f"ML_MaterialMixer_BleedCoord_{idx}")
                coord.label="Bleed • Target Coordinates"
                coord.parent=frame
                coord.location=(-1500,y-380)
                coord["ML_MIXER_GENERATED"]=True
                coord["ML_MIXER_BLEED_COORD_INDEX"]=idx
                try:
                    coord.object=layer.bleed_object if layer.bleed_object else None
                except Exception:
                    pass

                grad_mapping=nodes.new("ShaderNodeMapping")
                grad_mapping.name=_unique_node_name(mat,f"ML_MaterialMixer_BleedMapping_{idx}")
                grad_mapping.label="Bleed • Target Mapping"
                grad_mapping.parent=frame
                grad_mapping.location=(-1280,y-380)
                grad_mapping["ML_MIXER_GENERATED"]=True
                grad_mapping["ML_MIXER_BLEED_MAPPING_INDEX"]=idx
                # Target local +Z is up; map it to negative X so below the target
                # becomes the white side of the linear gradient.
                grad_mapping.inputs["Rotation"].default_value[1]=-1.5707963267948966
                links.new(coord.outputs["Object"],grad_mapping.inputs["Vector"])

                gradient=nodes.new("ShaderNodeTexGradient")
                gradient.name=_unique_node_name(mat,f"ML_MaterialMixer_BleedGradient_{idx}")
                gradient.label="Bleed • Height"
                gradient.parent=frame
                gradient.location=(-1040,y-380)
                gradient.gradient_type='LINEAR'
                gradient["ML_MIXER_GENERATED"]=True
                gradient["ML_MIXER_BLEED_GRADIENT_INDEX"]=idx
                links.new(grad_mapping.outputs["Vector"],gradient.inputs["Vector"])

                noise_mapping=nodes.new("ShaderNodeMapping")
                noise_mapping.name=_unique_node_name(mat,f"ML_MaterialMixer_BleedNoiseMapping_{idx}")
                noise_mapping.label="Bleed • Noise Mapping"
                noise_mapping.parent=frame
                noise_mapping.location=(-1040,y-620)
                noise_mapping["ML_MIXER_GENERATED"]=True
                noise_mapping["ML_MIXER_BLEED_NOISE_MAPPING_INDEX"]=idx
                noise_mapping.inputs["Scale"].default_value=(float(layer.bleed_tiling),)*3
                links.new(coord.outputs["Object"],noise_mapping.inputs["Vector"])

                bleed_noise=nodes.new("ShaderNodeTexNoise")
                bleed_noise.name=_unique_node_name(mat,f"ML_MaterialMixer_BleedNoise_{idx}")
                bleed_noise.label="Bleed • Border Noise"
                bleed_noise.parent=frame
                bleed_noise.location=(-800,y-620)
                bleed_noise["ML_MIXER_GENERATED"]=True
                bleed_noise["ML_MIXER_BLEED_NOISE_INDEX"]=idx
                bleed_noise.noise_dimensions='4D'
                for name,value in (
                    ("Scale",layer.bleed_noise_scale),
                    ("Detail",layer.bleed_noise_detail),
                    ("Roughness",layer.bleed_noise_roughness),
                    ("Lacunarity",layer.bleed_noise_lacunarity),
                    ("W",layer.bleed_noise_seed),
                    ("Distortion",layer.bleed_noise_distortion),
                ):
                    if bleed_noise.inputs.get(name):
                        bleed_noise.inputs[name].default_value=float(value)
                try:
                    bleed_noise.normalize=bool(layer.bleed_noise_normalize)
                except Exception:
                    pass
                links.new(noise_mapping.outputs["Vector"],bleed_noise.inputs["Vector"])

                noise_ramp=nodes.new("ShaderNodeValToRGB")
                noise_ramp.name=_unique_node_name(mat,f"ML_MaterialMixer_BleedNoiseRamp_{idx}")
                noise_ramp.label="Bleed • Noise Contrast"
                noise_ramp.parent=frame
                noise_ramp.location=(-560,y-620)
                noise_ramp["ML_MIXER_GENERATED"]=True
                noise_ramp["ML_MIXER_BLEED_NOISE_RAMP_INDEX"]=idx
                _set_mixer_ramp(
                    noise_ramp.color_ramp,
                    layer.bleed_noise_ramp_black,
                    layer.bleed_noise_ramp_white
                )
                links.new(bleed_noise.outputs["Fac"],noise_ramp.inputs["Fac"])

                break_mult=nodes.new("ShaderNodeMath")
                break_mult.name=_unique_node_name(mat,f"ML_MaterialMixer_BleedBreakMultiply_{idx}")
                break_mult.label="Noise × Break Border"
                break_mult.parent=frame
                break_mult.location=(-320,y-620)
                break_mult.operation='MULTIPLY'
                break_mult.inputs[1].default_value=float(layer.bleed_break_border)
                break_mult["ML_MIXER_GENERATED"]=True
                break_mult["ML_MIXER_BLEED_BREAK_MULTIPLY_INDEX"]=idx
                links.new(noise_ramp.outputs["Color"],break_mult.inputs[0])

                break_sub=nodes.new("ShaderNodeMath")
                break_sub.name=_unique_node_name(mat,f"ML_MaterialMixer_BleedBreakSubtract_{idx}")
                break_sub.label="Height − Border Break"
                break_sub.parent=frame
                break_sub.location=(-80,y-380)
                break_sub.operation='SUBTRACT'
                break_sub.use_clamp=True
                break_sub["ML_MIXER_GENERATED"]=True
                break_sub["ML_MIXER_BLEED_BREAK_SUBTRACT_INDEX"]=idx
                links.new(gradient.outputs["Fac"],break_sub.inputs[0])
                links.new(break_mult.outputs["Value"],break_sub.inputs[1])

                bleed_ramp=nodes.new("ShaderNodeValToRGB")
                bleed_ramp.name=_unique_node_name(mat,f"ML_MaterialMixer_BleedRamp_{idx}")
                bleed_ramp.label="Bleed • Mask Contrast"
                bleed_ramp.parent=frame
                bleed_ramp.location=(180,y-380)
                bleed_ramp["ML_MIXER_GENERATED"]=True
                bleed_ramp["ML_MIXER_BLEED_RAMP_INDEX"]=idx
                _set_mixer_ramp(bleed_ramp.color_ramp,layer.bleed_ramp_black,layer.bleed_ramp_white)
                links.new(break_sub.outputs["Value"],bleed_ramp.inputs["Fac"])

                factor=nodes.new("ShaderNodeMath")
                factor.name=_unique_node_name(mat,f"ML_MaterialMixer_MaskFactor_{idx}")
                factor.label="Blend × Bleed"
                factor.parent=frame
                factor.location=(440,y-380)
                factor.operation='MULTIPLY'
                factor.inputs[1].default_value=(
                    0.0 if not layer.enabled
                    else (1.0 if layer.blend_mode=="REPLACE" else float(layer.amount))
                )
                factor["ML_MIXER_GENERATED"]=True
                factor["ML_MIXER_FACTOR_INDEX"]=idx
                links.new(bleed_ramp.outputs["Color"],factor.inputs[0])
                factor_socket=factor.outputs["Value"]


        # Signed Material Height: positive = raised layer, negative = recessed/cavity.
        if factor_socket and copied:
            copied_node=getattr(copied,"node",None)
            target_principled=None
            if copied_node and copied_node.type=="BSDF_PRINCIPLED":
                target_principled=copied_node
            else:
                target_principled=next(
                    (n for n in nodes if n.parent==source_frame and n.type=="BSDF_PRINCIPLED"),
                    None
                )

            if target_principled:
                height_invert=nodes.new("ShaderNodeMath")
                height_invert.name=_unique_node_name(mat,f"ML_MaterialMixer_HeightInvert_{idx}")
                height_invert.label="Material Height Invert"
                height_invert.parent=frame
                height_invert.location=(520,y-380)
                height_invert.operation='SUBTRACT'
                height_invert.inputs[0].default_value=1.0
                height_invert["ML_MIXER_GENERATED"]=True
                height_invert["ML_MIXER_HEIGHT_INVERT_INDEX"]=idx

                height_bump=nodes.new("ShaderNodeBump")
                height_bump.name=_unique_node_name(mat,f"ML_MaterialMixer_HeightBump_{idx}")
                height_bump.label="Material Height"
                height_bump.parent=frame
                height_bump.location=(760,y-380)
                height_bump.inputs["Strength"].default_value=abs(layer.height)
                height_bump.inputs["Distance"].default_value=0.08
                height_bump["ML_MIXER_GENERATED"]=True
                height_bump["ML_MIXER_HEIGHT_BUMP_INDEX"]=idx

                links.new(factor_socket,height_invert.inputs[1])
                if layer.height < 0.0:
                    links.new(height_invert.outputs["Value"],height_bump.inputs["Height"])
                else:
                    links.new(factor_socket,height_bump.inputs["Height"])

                normal_input=target_principled.inputs.get("Normal")
                if normal_input:
                    previous=list(normal_input.links)
                    if previous:
                        source=previous[0].from_socket
                        links.remove(previous[0])
                        links.new(source,height_bump.inputs["Normal"])
                    links.new(height_bump.outputs["Normal"],normal_input)

        mix=nodes.new("ShaderNodeMixShader")
        mix.name=_unique_node_name(mat,f"ML_MaterialMixer_Mix_{idx}")
        mix.label=f"Material {idx+2} • {layer.amount*100:.0f}%"
        mix.parent=frame
        mix.location=(1250,y)
        mix["ML_MIXER_GENERATED"]=True
        mix["ML_MIXER_MIX_INDEX"]=idx

        if factor_socket:
            links.new(factor_socket,mix.inputs[0])
        else:
            mix.inputs[0].default_value=(
                0.0 if not layer.enabled
                else (1.0 if layer.blend_mode=="REPLACE" else float(layer.amount))
            )

        links.new(current,mix.inputs[1])
        links.new(copied,mix.inputs[2])
        current=mix.outputs["Shader"]

    for n in nodes:
        for prop in (
            "ML_MIXER_SOURCE_INDEX",
            "ML_MIXER_MASK_COORD_INDEX",
            "ML_MIXER_MASK_MAPPING_INDEX",
            "ML_MIXER_MASK_NOISE_INDEX",
            "ML_MIXER_MASK_IMAGE_INDEX",
            "ML_MIXER_MASK_RAMP_INDEX",
            "ML_MIXER_BLEED_COORD_INDEX",
            "ML_MIXER_BLEED_MAPPING_INDEX",
            "ML_MIXER_BLEED_GRADIENT_INDEX",
            "ML_MIXER_BLEED_RAMP_INDEX",
            "ML_MIXER_BLEED_NOISE_MAPPING_INDEX",
            "ML_MIXER_BLEED_NOISE_INDEX",
            "ML_MIXER_BLEED_NOISE_RAMP_INDEX",
            "ML_MIXER_BLEED_BREAK_MULTIPLY_INDEX",
            "ML_MIXER_BLEED_BREAK_SUBTRACT_INDEX",
            "ML_MIXER_FACTOR_INDEX",
            "ML_MIXER_HEIGHT_INVERT_INDEX",
            "ML_MIXER_HEIGHT_BUMP_INDEX",
            "ML_MIXER_MIX_INDEX",
        ):
            if prop in n:
                key=(prop,int(n.get(prop)))
                if key in saved_positions:
                    n.location=saved_positions[key]
                break

    if surface:
        for l in list(surface.links):
            links.remove(l)

        if bool(frame.get("ML_ENABLED",True)):
            links.new(current,surface)
        else:
            orig_node=nodes.get(frame.get("ML_ORIG_SURFACE_NODE",""))
            orig_socket=(
                orig_node.outputs.get(frame.get("ML_ORIG_SURFACE_SOCKET",""))
                if orig_node else None
            )
            if orig_socket:
                links.new(orig_socket,surface)

    frame["ML_ENABLED"]=bool(frame.get("ML_ENABLED",True))
    return True




# ------------------------------------------------------------------------
# Material Mixer
# ------------------------------------------------------------------------

class ML_OT_mixer_add_layer(Operator):
    bl_idname="material_lab.mixer_add_layer"
    bl_label="Add Material"
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        obj=context.active_object; mat=obj.active_material if obj else None
        if not mat: return {'CANCELLED'}
        if not _effect_nodes(mat,"Material Mixer"):
            bpy.ops.material_lab.add_look(effect="Material Mixer")
        layers=mat.material_lab_settings.mixer_layers
        if len(layers)>=6:
            self.report({'WARNING'},"The Mixer supports up to 6 added materials.")
            return {'CANCELLED'}
        layer=layers.add()
        layer.blend_mode="MIX"
        layer.amount=0.5
        layer.height=0.0
        layer.enabled=True
        layer.mask_source="NONE"
        layer.mask_mapping_source="Generated"
        layer.mask_tiling=1.0
        layer.mask_noise_scale=1.3
        layer.mask_noise_detail=15.0
        layer.mask_noise_roughness=0.6
        layer.mask_noise_lacunarity=2.0
        layer.mask_noise_seed=1.0
        layer.mask_noise_distortion=0.0
        layer.mask_noise_normalize=False
        layer.mask_contrast=0.08
        layer.mask_ramp_black=0.0
        layer.mask_ramp_white=0.30
        layer.bleed_break_border=0.0
        layer.bleed_tiling=1.0
        layer.bleed_noise_scale=4.0
        layer.bleed_noise_detail=8.0
        layer.bleed_noise_roughness=0.7
        layer.bleed_noise_lacunarity=2.0
        layer.bleed_noise_seed=1.0
        layer.bleed_noise_distortion=0.35
        layer.bleed_noise_normalize=False
        layer.bleed_noise_collapsed=False
        layer.bleed_ramp_black=0.0
        layer.bleed_ramp_white=0.30
        layer.bleed_noise_ramp_black=0.0
        layer.bleed_noise_ramp_white=1.0
        mat.material_lab_settings.mixer_active_index=len(layers)-1
        _rebuild_material_mixer(mat)
        return {'FINISHED'}


class ML_OT_mixer_create_bleed_target(Operator):
    bl_idname="material_lab.mixer_create_bleed_target"
    bl_label="Create Target"
    bl_options={'REGISTER','UNDO'}

    index:IntProperty(default=0)

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj else None
        if not mat or obj.type!="MESH":
            return {'CANCELLED'}

        layers=mat.material_lab_settings.mixer_layers
        if not (0 <= self.index < len(layers)):
            return {'CANCELLED'}

        data=bpy.data.curves.new(name="ML_Bleed_Target_Curve",type='CURVE')
        data.dimensions='3D'
        data.resolution_u=1
        data.bevel_depth=0.0
        data.bevel_resolution=2
        data.fill_mode='FULL'

        # Small, closed rig-style controller.
        # Local +Z is the arrow direction and the origin is centered vertically.
        outline=[
            (-0.040,0.0,-0.42),
            ( 0.040,0.0,-0.42),
            ( 0.040,0.0, 0.10),
            ( 0.120,0.0, 0.10),
            ( 0.000,0.0, 0.42),
            (-0.120,0.0, 0.10),
            (-0.040,0.0, 0.10),
        ]

        spline=data.splines.new('POLY')
        spline.points.add(len(outline)-1)
        spline.use_cyclic_u=True
        for point,co in zip(spline.points,outline):
            point.co=(co[0],co[1],co[2],1.0)

        target=bpy.data.objects.new("ML_Bleed_Target",data)
        bpy.context.collection.objects.link(target)
        target.location=obj.matrix_world.translation
        target.rotation_euler=(0.0,0.0,0.0)
        target["ML_BLEED_TARGET"]=True
        target["ML_BLEED_TARGET_INDEX"]=int(self.index)
        target.hide_render=True
        try:
            target.show_in_front=True
        except Exception:
            pass

        if bpy.data.objects.get(target.name) and bpy.data.objects.get(target.name) != target:
            base_name="ML_Bleed_Target"
            n=1
            while bpy.data.objects.get(f"{base_name}_{n:02d}"):
                n+=1
            target.name=f"{base_name}_{n:02d}"

        layers[self.index].bleed_object=target
        layers[self.index].mask_source="BLEED"
        mat.material_lab_settings.mixer_active_index=self.index
        _rebuild_material_mixer(mat)

        try:
            bpy.ops.object.select_all(action='DESELECT')
            target.select_set(True)
            bpy.context.view_layer.objects.active=target
        except Exception:
            pass

        return {'FINISHED'}


class ML_OT_mixer_remove_layer(Operator):
    bl_idname="material_lab.mixer_remove_layer"
    bl_label="Remove Material"
    bl_options={'REGISTER','UNDO'}
    index:IntProperty(default=0)
    def execute(self,context):
        obj=context.active_object; mat=obj.active_material if obj else None
        if not mat: return {'CANCELLED'}
        layers=mat.material_lab_settings.mixer_layers
        if 0<=self.index<len(layers):
            layers.remove(self.index)
            mat.material_lab_settings.mixer_active_index=max(0,min(self.index,len(layers)-1))
            _rebuild_material_mixer(mat)
            return {'FINISHED'}
        return {'CANCELLED'}


class ML_OT_mixer_move_layer(Operator):
    bl_idname="material_lab.mixer_move_layer"
    bl_label="Move Material"
    bl_options={'REGISTER','UNDO'}
    direction:IntProperty(default=1); index:IntProperty(default=0)
    def execute(self,context):
        obj=context.active_object; mat=obj.active_material if obj else None
        if not mat: return {'CANCELLED'}
        layers=mat.material_lab_settings.mixer_layers
        ni=self.index+self.direction
        if 0<=self.index<len(layers) and 0<=ni<len(layers):
            layers.move(self.index,ni)
            mat.material_lab_settings.mixer_active_index=ni
            _rebuild_material_mixer(mat)
            return {'FINISHED'}
        return {'CANCELLED'}


class ML_OT_mixer_toggle_layer(Operator):
    bl_idname="material_lab.mixer_toggle_layer"
    bl_label="Toggle Material"
    bl_options={'REGISTER','UNDO'}
    index:IntProperty(default=0)
    def execute(self,context):
        obj=context.active_object; mat=obj.active_material if obj else None
        if not mat: return {'CANCELLED'}
        layers=mat.material_lab_settings.mixer_layers
        if 0<=self.index<len(layers):
            layers[self.index].enabled=not layers[self.index].enabled
            _update_mixer_layer_nodes_in_place(mat,self.index)
            return {'FINISHED'}
        return {'CANCELLED'}


class ML_OT_mixer_build(Operator):
    bl_idname="material_lab.mixer_build"
    bl_label="Build Mixer"
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        obj=context.active_object; mat=obj.active_material if obj else None
        if not mat: return {'CANCELLED'}
        if _rebuild_material_mixer(mat):
            self.report({'INFO'},"Material Mixer actualizado.")
            return {'FINISHED'}
        self.report({'WARNING'},"Could not build the Material Mixer.")
        return {'CANCELLED'}


# ------------------------------------------------------------------------
# UI
# ------------------------------------------------------------------------

# ------------------------------------------------------------------------
# UI
# ------------------------------------------------------------------------


def _mixer_mask_image_node(mat, idx):
    if not mat or not mat.node_tree or idx < 0:
        return None
    return next(
        (n for n in mat.node_tree.nodes
         if n.get("ML_MIXER_MASK_IMAGE_INDEX")==idx and n.type=="TEX_IMAGE"),
        None
    )



def _mixer_get_paint_mode_settings(context):
    tool_settings=getattr(context,"tool_settings",None)
    return getattr(tool_settings,"paint_mode",None) if tool_settings else None


def _mixer_get_image_paint_settings(context):
    tool_settings=getattr(context,"tool_settings",None)
    return getattr(tool_settings,"image_paint",None) if tool_settings else None



def _mixer_set_live_paint_white(context):
    """Set the active Image Paint brush color to white."""
    try:
        tool_settings = context.scene.tool_settings
        image_paint = getattr(tool_settings, "image_paint", None)
        if image_paint is None:
            return

        brush = getattr(image_paint, "brush", None)
        if brush is not None and hasattr(brush, "color"):
            brush.color = (1.0, 1.0, 1.0)

        # Blender 5.2 stores Unified Paint Settings on Image Paint.
        unified = getattr(image_paint, "unified_paint_settings", None)
        if unified is not None and hasattr(unified, "color"):
            try:
                unified.use_unified_color=True
            except Exception:
                pass
            unified.color = (1.0, 1.0, 1.0)
    except Exception:
        pass


def _mixer_set_paint_canvas(context, image):
    """Bind the Mixer image to Blender's current Texture Paint session."""
    ok=False

    paint_mode=_mixer_get_paint_mode_settings(context)
    if paint_mode is not None:
        try:
            paint_mode.canvas_source='IMAGE'
            paint_mode.canvas_image=image
            ok=(paint_mode.canvas_source=='IMAGE' and paint_mode.canvas_image==image)
        except Exception:
            pass

    image_paint=_mixer_get_image_paint_settings(context)
    if image_paint is not None:
        try:
            image_paint.mode='IMAGE'
            ok=True
        except Exception:
            pass
        try:
            image_paint.canvas=image
            ok=True
        except Exception:
            pass

    return ok


def _mixer_restore_paint_canvas(context, settings):
    ok=False
    previous=settings.mixer_prev_canvas_image
    previous_source=settings.mixer_prev_canvas_source or 'MATERIAL'

    paint_mode=_mixer_get_paint_mode_settings(context)
    if paint_mode is not None:
        try:
            paint_mode.canvas_source=previous_source
            paint_mode.canvas_image=previous
            ok=True
        except Exception:
            pass

    image_paint=_mixer_get_image_paint_settings(context)
    if image_paint is not None:
        try:
            if previous is not None and previous_source=='IMAGE':
                image_paint.mode='IMAGE'
                image_paint.canvas=previous
            else:
                image_paint.mode='MATERIAL'
                try:
                    image_paint.canvas=None
                except Exception:
                    pass
            ok=True
        except Exception:
            pass

    return ok


def _mixer_layer_has_uv(obj):
    return bool(obj and obj.type=="MESH" and getattr(obj.data,"uv_layers",None) and len(obj.data.uv_layers)>0)


class ML_OT_create_mixer_mask_texture(Operator):
    bl_idname="material_lab.create_mixer_mask_texture"
    bl_label="Create Texture"
    bl_options={'REGISTER','UNDO'}

    index:IntProperty(default=-1)

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj and obj.type=="MESH" else None
        if not mat:
            return {'CANCELLED'}
        if not (0 <= self.index < len(mat.material_lab_settings.mixer_layers)):
            return {'CANCELLED'}
        if not _mixer_layer_has_uv(obj):
            self.report({'WARNING'},"The object needs a UV map to paint a mask texture.")
            return {'CANCELLED'}

        layer=mat.material_lab_settings.mixer_layers[self.index]

        # A newly created Live Paint texture uses the object's UV map by default.
        layer.mask_mapping_source="UV"

        base_name=f"{mat.name}_MixerMask_{self.index+2}"
        image=bpy.data.images.new(
            name=bpy.data.images.get(base_name).name if bpy.data.images.get(base_name) else base_name,
            width=1024,
            height=1024,
            alpha=True,
            float_buffer=False,
        )
        try:
            image.generated_color=(0.0,0.0,0.0,1.0)
        except Exception:
            pass
        try:
            image.colorspace_settings.name='Non-Color'
        except Exception:
            pass
        image.use_fake_user=False

        layer.mask_image=image
        layer.mask_source="TEXTURE"
        _rebuild_material_mixer(mat)

        self.report({'INFO'},f"Created mask texture: {image.name}")
        return {'FINISHED'}


class ML_OT_paint_mixer_mask_texture(Operator):
    bl_idname="material_lab.paint_mixer_mask_texture"
    bl_label="Live Paint"
    bl_options={'REGISTER','UNDO'}

    index:IntProperty(default=-1)

    def execute(self,context):
        obj=context.active_object
        mat=obj.active_material if obj and obj.type=="MESH" else None
        if not mat:
            return {'CANCELLED'}
        if not (0 <= self.index < len(mat.material_lab_settings.mixer_layers)):
            return {'CANCELLED'}
        if not _mixer_layer_has_uv(obj):
            self.report({'WARNING'},"The object needs a UV map to paint a mask texture.")
            return {'CANCELLED'}

        layer=mat.material_lab_settings.mixer_layers[self.index]
        image=layer.mask_image
        image_node=_mixer_mask_image_node(mat,self.index)
        if not image or not image_node:
            self.report({'WARNING'},"Create a mask texture first.")
            return {'CANCELLED'}

        settings=mat.material_lab_settings
        paint=_mixer_get_image_paint_settings(context)

        # Toggle off only when this exact layer is currently being painted.
        if obj.mode=="TEXTURE_PAINT" and settings.mixer_paint_layer==self.index:
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception as exc:
                self.report({'WARNING'},f"Could not leave Texture Paint mode: {exc}")
                return {'CANCELLED'}
            _mixer_restore_paint_canvas(context,settings)
            settings.mixer_paint_layer=-1
            return {'FINISHED'}

        # Preserve the earlier working behavior: the Mixer image texture node
        # is made active before entering Texture Paint. Blender can use that
        # active image slot even when direct canvas assignment is unavailable.
        nodes=mat.node_tree.nodes
        for node in nodes:
            node.select=False
        image_node.select=True
        nodes.active=image_node

        try:
            context.view_layer.objects.active=obj
            obj.select_set(True)
        except Exception:
            pass

        # Enter Texture Paint before assigning PaintModeSettings. In Blender
        # 5.2 PaintModeSettings is the documented owner of canvas_source and
        # canvas_image and becomes available with the paint session active.
        if obj.mode!="TEXTURE_PAINT":
            settings.mixer_prev_canvas_source='MATERIAL'
            settings.mixer_prev_canvas_image=None
            try:
                bpy.ops.object.mode_set(mode='TEXTURE_PAINT')
            except Exception as exc:
                self.report({'WARNING'},f"Could not enter Texture Paint mode: {exc}")
                return {'CANCELLED'}
        else:
            pm=_mixer_get_paint_mode_settings(context)
            if pm is not None:
                try:
                    settings.mixer_prev_canvas_source=pm.canvas_source
                    settings.mixer_prev_canvas_image=pm.canvas_image
                except Exception:
                    settings.mixer_prev_canvas_source='MATERIAL'
                    settings.mixer_prev_canvas_image=None

        # Use the existing Mixer image directly. Do NOT create paint slots,
        # duplicate images, or temporary materials. Blender 5.2 supports an
        # existing Image Texture node as the paint target when it is active.
        if not _mixer_set_paint_canvas(context,image):
            self.report({'WARNING'},"Could not set the Mixer mask as the Texture Paint canvas.")
            try: bpy.ops.object.mode_set(mode='OBJECT')
            except Exception: pass
            return {'CANCELLED'}

        # Re-activate the SAME Mixer Image Texture after entering paint mode.
        # This is important because Blender refreshes paint slots on mode entry.
        nodes=mat.node_tree.nodes
        for node in nodes: node.select=False
        image_node.select=True
        nodes.active=image_node

        # Mixer masks start each Live Paint session with a white brush.
        layer.live_paint_value=1.0
        _mixer_set_live_paint_white(context)
        # Keep Unified Color explicitly enabled: in Blender 5.2 it is the
        # effective Image Paint color when enabled.
        try:
            paint=_mixer_get_image_paint_settings(context)
            unified=getattr(paint,"unified_paint_settings",None) if paint else None
            if unified is not None:
                unified.use_unified_color=True
                unified.color=(1.0,1.0,1.0)
        except Exception:
            pass

        settings.mixer_paint_layer=self.index
        return {'FINISHED'}



def _draw_material_mixer_panel(layout, mat, obj, context):
    settings=mat.material_lab_settings
    frame=_find_effect_frame(mat,"Material Mixer")

    box=layout
    header=box.row(align=True)
    if frame:
        header.prop(settings,"mixer_collapsed",text="",icon='TRIA_RIGHT' if settings.mixer_collapsed else 'TRIA_DOWN',emboss=False)
        header.label(text="Material Mixer",icon='NODE_MATERIAL')
        op=header.operator("material_lab.reset_look",text="",icon='FILE_REFRESH',emboss=False); op.effect="Material Mixer"
        enabled=bool(frame.get("ML_ENABLED",True))
        op=header.operator("material_lab.toggle_look",text="",icon='HIDE_OFF' if enabled else 'HIDE_ON',emboss=False); op.effect="Material Mixer"
        op=header.operator("material_lab.remove_look",text="",icon='X',emboss=False); op.effect="Material Mixer"

        if settings.mixer_collapsed:
            return

        eb=box.box()
        eb.label(text="MATERIAL LAYERS",icon='NODE_MATERIAL')
        for i,layer in enumerate(settings.mixer_layers):
            card=eb.box()
            hdr=card.row(align=True)
            tog=hdr.operator("material_lab.mixer_toggle_layer",text="",icon='HIDE_OFF' if layer.enabled else 'HIDE_ON',emboss=False); tog.index=i
            hdr.label(text=f"Material {i+2}")
            up=hdr.operator("material_lab.mixer_move_layer",text="",icon='TRIA_UP',emboss=False); up.direction=-1; up.index=i
            down=hdr.operator("material_lab.mixer_move_layer",text="",icon='TRIA_DOWN',emboss=False); down.direction=1; down.index=i
            rem=hdr.operator("material_lab.mixer_remove_layer",text="",icon='X',emboss=False); rem.index=i

            card.prop(layer,"material",text="Material")
            card.prop(layer,"blend_mode",text="Mode")
            blend_row=card.row()
            blend_row.enabled=(layer.blend_mode=="MIX")
            blend_row.prop(layer,"amount",text="Blend",slider=True)

            mrow=card.row(align=True)
            mrow.label(text="MASK",icon='MOD_NOISE')
            mrow.prop(layer,"mask_source",text="")
            if _mixer_mask_socket(mat,i):
                vo=mrow.operator("material_lab.view_mask",text="View Mask",icon='HIDE_ON' if layer.view_mask else 'HIDE_OFF')
                vo.effect="Material Mixer"; vo.index=i

            if layer.mask_source=="TEXTURE":
                # Compact texture row: image + all texture actions stay together.
                ir=card.row(align=True)
                ir.prop(layer,"mask_image",text="")

                if layer.mask_image:
                    ir.prop(
                        layer.mask_image,
                        "use_fake_user",
                        text="",
                        icon='FAKE_USER_ON' if layer.mask_image.use_fake_user else 'FAKE_USER_OFF',
                        emboss=False,
                    )

                create=ir.operator("material_lab.create_mixer_mask_texture", text="", icon='ADD')
                create.index=i

                op=ir.operator("material_lab.open_effect_image", text="", icon='FILE_FOLDER')
                op.effect="Material Mixer"; op.slot=f"MASK:{i}"

                rnd=ir.operator("material_lab.random_image", text="", icon='FILE_REFRESH')
                rnd.effect="Material Mixer"; rnd.slot=f"MASK:{i}"

                if layer.mask_image:
                    is_painting=(
                        obj is not None
                        and obj.mode=="TEXTURE_PAINT"
                        and mat.material_lab_settings.mixer_paint_layer==i
                    )
                    paint_row=card.row(align=True)
                    paint=paint_row.operator(
                        "material_lab.paint_mixer_mask_texture",
                        text="Live Paint",
                        icon='BRUSH_DATA',
                        depress=is_painting,
                    )
                    paint.index=i

                    if is_painting:
                        # Horizontal black-to-white control for the real Image Paint brush.
                        slider=card.row(align=True)
                        slider.alignment='EXPAND'
                        slider.prop(layer,"live_paint_value",text="Value",slider=True)

                ramp=next((n for n in mat.node_tree.nodes if n.get("ML_MIXER_MASK_RAMP_INDEX")==i),None)
                if ramp:
                    rr=card.row(align=True)
                    rr.prop(layer,"mask_ramp_collapsed",text="",icon='TRIA_RIGHT' if layer.mask_ramp_collapsed else 'TRIA_DOWN',emboss=False)
                    rr.label(text="Mask Contrast")
                    if not layer.mask_ramp_collapsed:
                        card.template_color_ramp(ramp,"color_ramp",expand=True)
                card.prop(layer,"mask_mapping_source",text="Mapping")
                if layer.mask_mapping_source=="Object":
                    card.prop(layer,"mask_object",text="Object")
                card.prop(layer,"mask_tiling",text="Tiling",slider=True)

            elif layer.mask_source=="NOISE":
                nr=card.row(align=True)
                nr.prop(layer,"mask_noise_collapsed",text="",icon='TRIA_RIGHT' if layer.mask_noise_collapsed else 'TRIA_DOWN',emboss=False)
                nr.label(text="Noise")
                rr=nr.operator("material_lab.randomize_noise",text="Randomize",icon='FILE_REFRESH'); rr.effect="Material Mixer"; rr.index=i
                if not layer.mask_noise_collapsed:
                    for prop_name,label in (("mask_noise_scale","Scale"),("mask_noise_detail","Detail"),("mask_noise_roughness","Roughness"),("mask_noise_lacunarity","Lacunarity"),("mask_noise_seed","Seed"),("mask_noise_distortion","Distortion")):
                        card.prop(layer,prop_name,text=label,slider=True)
                    card.prop(layer,"mask_noise_normalize",text="Normalize")
                    ramp=next((n for n in mat.node_tree.nodes if n.get("ML_MIXER_MASK_RAMP_INDEX")==i),None)
                    if ramp:
                        rr=card.row(align=True)
                        rr.prop(layer,"mask_ramp_collapsed",text="",icon='TRIA_RIGHT' if layer.mask_ramp_collapsed else 'TRIA_DOWN',emboss=False)
                        rr.label(text="Mask Contrast")
                        if not layer.mask_ramp_collapsed:
                            card.template_color_ramp(ramp,"color_ramp",expand=True)
                    card.prop(layer,"mask_mapping_source",text="Mapping")
                    if layer.mask_mapping_source=="Object":
                        card.prop(layer,"mask_object",text="Object")
                    card.prop(layer,"mask_tiling",text="Tiling",slider=True)

            elif layer.mask_source=="BLEED":
                tr=card.row(align=True)
                tr.prop(layer,"bleed_object",text="Target")
                ct=tr.operator("material_lab.mixer_create_bleed_target",text="Create Target",icon='CURVE_DATA')
                ct.index=i
                card.prop(layer,"bleed_break_border",text="Break Border",slider=True)

                br=card.row(align=True)
                br.label(text="Bleed")
                bleed_ramp=next((n for n in mat.node_tree.nodes if n.get("ML_MIXER_BLEED_RAMP_INDEX")==i),None)
                if bleed_ramp:
                    br.prop(layer,"bleed_ramp_collapsed",text="",icon='TRIA_RIGHT' if layer.bleed_ramp_collapsed else 'TRIA_DOWN',emboss=False)
                    br.label(text="Mask Contrast")
                    if not layer.bleed_ramp_collapsed:
                        card.template_color_ramp(bleed_ramp,"color_ramp",expand=True)

                nr=card.row(align=True)
                nr.prop(layer,"bleed_noise_collapsed",text="",icon='TRIA_RIGHT' if layer.bleed_noise_collapsed else 'TRIA_DOWN',emboss=False)
                nr.label(text="Noise")
                rr=nr.operator("material_lab.randomize_noise",text="Randomize",icon='FILE_REFRESH'); rr.effect="Material Mixer"; rr.index=i
                if not layer.bleed_noise_collapsed:
                    for prop_name,label in (
                        ("bleed_noise_scale","Scale"),
                        ("bleed_noise_detail","Detail"),
                        ("bleed_noise_roughness","Roughness"),
                        ("bleed_noise_lacunarity","Lacunarity"),
                        ("bleed_noise_seed","Seed"),
                        ("bleed_noise_distortion","Distortion"),
                    ):
                        card.prop(layer,prop_name,text=label,slider=True)
                    card.prop(layer,"bleed_noise_normalize",text="Normalize")
                    noise_ramp=next((n for n in mat.node_tree.nodes if n.get("ML_MIXER_BLEED_NOISE_RAMP_INDEX")==i),None)
                    if noise_ramp:
                        rr=card.row(align=True)
                        rr.prop(layer,"bleed_noise_ramp_collapsed",text="",icon='TRIA_RIGHT' if layer.bleed_noise_ramp_collapsed else 'TRIA_DOWN',emboss=False)
                        rr.label(text="Noise Contrast")
                        if not layer.bleed_noise_ramp_collapsed:
                            card.template_color_ramp(noise_ramp,"color_ramp",expand=True)
                    card.prop(layer,"bleed_tiling",text="Noise Tiling",slider=True)

            card.prop(layer,"height",text="Height",slider=True)

        if len(settings.mixer_layers)<6:
            eb.operator("material_lab.mixer_add_layer",text="Add Material",icon='ADD')
        eb.label(text="The active material is always the Base.",icon='INFO')
    else:
        header.label(text="Material Mixer",icon='NODE_MATERIAL')
        op=header.operator("material_lab.add_look",text="Add Material Mixer",icon='ADD',emboss=True)
        op.effect="Material Mixer"


class ML_PT_main(Panel):
    bl_label = "ShaderPit"
    bl_idname = "ML_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "ShaderPit"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        # ================================================================
        # 1. OBJECT MATERIALS — Blender-like material workflow
        # ================================================================
        box = layout.box()
        box.label(text="Object Materials", icon='MATERIAL')

        if obj is None or obj.type != 'MESH':
            box.label(text="Select a Mesh object.", icon='INFO')
        else:
            # Blender-like datablock selector + Fake User shield.
            row=box.row(align=True)
            row.template_ID(obj,"active_material",new="material_lab.create_material")

            # Native Blender material slot list.
            row=box.row()
            row.template_list(
                "MATERIAL_UL_matslots",
                "",
                obj,
                "material_slots",
                obj,
                "active_material_index",
                rows=3
            )

            col=row.column(align=True)
            col.operator("object.material_slot_add",text="",icon='ADD')
            col.operator("object.material_slot_remove",text="",icon='REMOVE')

            if context.mode=="EDIT_MESH" and obj.active_material:
                row=box.row(align=True)
                row.operator("object.material_slot_assign",text="Asignar")
                row.operator("object.material_slot_select",text="Seleccionar")
                row.operator("object.material_slot_deselect",text="Deseleccionar")

            # Material Lab global tiling/coordinates.
            if obj.active_material:
                settings=obj.active_material.material_lab_settings
                row=box.row(align=True)
                row.prop(settings,"global_tiling",text="Tiling",slider=True)
                row.operator("material_lab.reset_material_tiling",text="",icon='FILE_REFRESH')
                row.prop(settings,"global_coordinate_source",text="Mapping")

        # SHADERPIT TOOLS — Effects / Material Mixer tabs.
        if obj and obj.type=='MESH' and obj.active_material:
            mat=obj.active_material
            settings=mat.material_lab_settings

            # Primary navigation sits above the section content.
            # This makes Effects / Mixer a true hierarchy level of its own.
            tab_row=layout.row(align=True)
            tab_row.scale_y=1.15
            tab_row.prop_enum(settings,"shaderpit_tab","EFFECTS",text="Effects")
            tab_row.prop_enum(settings,"shaderpit_tab","MIXER",text="Mixer")

            content_box=layout.box()

            if settings.shaderpit_tab=="MIXER":
                _draw_material_mixer_panel(content_box, mat, obj, context)
            else:
                tools_box=content_box
                existing=[e for e in _effect_ui_order(mat) if _effect_nodes(mat,e)]

                add_row=tools_box.row(align=True)
                for effect in ("Grunge","Dirt","Roughness"):
                    op=add_row.operator("material_lab.add_look",text=f"+ {effect}")
                    op.effect=effect

                add_row=tools_box.row(align=True)
                for effect in ("AO","HSV"):
                    label="Color" if effect=="HSV" else effect
                    op=add_row.operator("material_lab.add_look",text=f"+ {label}")
                    op.effect=effect

                if existing:
                    tools_box.separator()
                    header=tools_box.row(align=True)
                    header.label(text="Active Effects",icon='MODIFIER')
                    header.operator("material_lab.collapse_all_effects",text="Collapse All",icon='TRIA_RIGHT')

                    for effect in existing:
                        effect_nodes=_effect_nodes(mat,effect)
                        frame=_find_effect_frame(mat,effect)
                        enabled=bool(frame.get("ML_ENABLED",True)) if frame else True
                        collapsed=bool(frame.get("ML_COLLAPSED",False)) if frame else False
                        effect_index=existing.index(effect)

                        row=tools_box.row(align=True)
                        op=row.operator(
                            "material_lab.toggle_collapse",
                            text="",
                            icon='TRIA_RIGHT' if collapsed else 'TRIA_DOWN',
                            emboss=False
                        )
                        op.effect=effect
                        row.label(text=("Color" if effect=="HSV" else effect),icon='NODE')

                        up_row=row.row(align=True)
                        up_row.enabled=(effect_index > 0)
                        up=up_row.operator("material_lab.move_effect",text="",icon='TRIA_UP',emboss=False)
                        up.effect=effect; up.direction=-1
                        down_row=row.row(align=True)
                        down_row.enabled=(effect_index < len(existing)-1)
                        down=down_row.operator("material_lab.move_effect",text="",icon='TRIA_DOWN',emboss=False)
                        down.effect=effect; down.direction=1

                        op=row.operator("material_lab.reset_look",text="",icon='FILE_REFRESH')
                        op.effect=effect
                        op=row.operator("material_lab.toggle_look",text="",icon='HIDE_OFF' if enabled else 'HIDE_ON')
                        op.effect=effect
                        op=row.operator("material_lab.remove_look",text="",icon='X')
                        op.effect=effect

                        if collapsed:
                            continue

                        eb=tools_box.box()

                        if effect=="Grunge":
                            # MASK
                            sec=eb.box()
                            mh=sec.row(align=True)
                            gc=mh.operator(
                                "material_lab.toggle_effect_group",
                                text="",
                                icon='TRIA_RIGHT' if _effect_group_collapsed(frame,"MASK") else 'TRIA_DOWN',
                                emboss=False
                            )
                            gc.effect="Grunge"; gc.group="MASK"
                            mh.label(text="MASK",icon='MOD_NOISE')
                            vo=mh.operator("material_lab.view_mask",text="View Mask",icon='HIDE_ON' if settings.grunge_view_mask else 'HIDE_OFF')
                            vo.effect="Grunge"; vo.index=-1
                            if not _effect_group_collapsed(frame,"MASK"):
                                r=sec.row(align=True)
                                r.prop(settings,"grunge_alpha_source",text="Source")
                                if settings.grunge_alpha_source=="TEXTURE":
                                    r2=sec.row(align=True)
                                    r2.prop(settings,"grunge_alpha_image",text="Texture")
                                    op=r2.operator("material_lab.open_effect_image",text="",icon='FILE_FOLDER'); op.effect="Grunge"; op.slot="ALPHA"
                                    rnd=r2.operator("material_lab.random_image",text="",icon='FILE_REFRESH'); rnd.effect="Grunge"; rnd.slot="ALPHA"
                                if settings.grunge_alpha_source=="NOISE":
                                    nr=sec.row(align=True)
                                    nc=nr.operator("material_lab.toggle_noise_collapse",text="",icon='TRIA_RIGHT' if settings.grunge_noise_collapsed else 'TRIA_DOWN',emboss=False)
                                    nc.effect="Grunge"
                                    nr.label(text="Noise")
                                    rr=nr.operator("material_lab.randomize_noise",text="Randomize",icon='FILE_REFRESH'); rr.effect="Grunge"
                                    nr.operator("material_lab.reset_grunge_noise",text="Reset",icon='LOOP_BACK')
                                    if not settings.grunge_noise_collapsed:
                                        for prop_name,label in (
                                            ("grunge_noise_scale","Scale"),
                                            ("grunge_noise_detail","Detail"),
                                            ("grunge_noise_roughness","Roughness"),
                                            ("grunge_noise_lacunarity","Lacunarity"),
                                            ("grunge_noise_seed","Seed"),
                                            ("grunge_noise_distortion","Distortion"),
                                        ):
                                            sec.prop(settings,prop_name,text=label,slider=True)
                                        sec.prop(settings,"grunge_noise_normalize",text="Normalize")
                                elif settings.grunge_alpha_source=="TEXTURE":
                                    sec.label(text="Texture used as mask.",icon='IMAGE_DATA')
                                else:
                                    sec.label(text="No mask: Amount affects the entire surface.",icon='INFO')

                                ramp=_find_effect_node(mat,effect,"AlphaRamp")
                                if ramp:
                                    rr=sec.row(align=True)
                                    rr.prop(settings,"grunge_mask_ramp_collapsed",text="",icon='TRIA_RIGHT' if settings.grunge_mask_ramp_collapsed else 'TRIA_DOWN',emboss=False)
                                    rr.label(text="Mask Contrast")
                                    if not settings.grunge_mask_ramp_collapsed:
                                        sec.template_color_ramp(ramp,"color_ramp",expand=True)
                                sec.prop(settings,"grunge_alpha_mapping_source",text="Mapping")
                                sec.prop(settings,"grunge_alpha_tiling",text="Tiling",slider=True)

                            # DIFFUSE
                            sec=eb.box()
                            mh=sec.row(align=True)
                            gc=mh.operator(
                                "material_lab.toggle_effect_group",
                                text="",
                                icon='TRIA_RIGHT' if _effect_group_collapsed(frame,"DIFFUSE") else 'TRIA_DOWN',
                                emboss=False
                            )
                            gc.effect="Grunge"; gc.group="DIFFUSE"
                            mh.label(text="DIFFUSE",icon='COLOR')
                            if not _effect_group_collapsed(frame,"DIFFUSE"):
                                r=sec.row(align=True)
                                r.prop(settings,"grunge_color_source",text="Source")
                                if settings.grunge_color_source=="COLOR":
                                    color=_find_effect_node(mat,effect,"Color")
                                    if color:
                                        r2=sec.row(); r2.prop(color.outputs["Color"],"default_value",text="Color")
                                else:
                                    r2=sec.row(align=True)
                                    r2.prop(settings,"grunge_color_image",text="Texture")
                                    op=r2.operator("material_lab.open_effect_image",text="",icon='FILE_FOLDER'); op.effect="Grunge"; op.slot="COLOR"
                                    rnd=r2.operator("material_lab.random_image",text="",icon='FILE_REFRESH'); rnd.effect="Grunge"; rnd.slot="COLOR"
                                sec.prop(settings,"grunge_blending",text="Blending",slider=True)
                                sec.prop(settings,"grunge_amount",text="Amount",slider=True)
                                sec.prop(settings,"grunge_color_mapping_source",text="Mapping")
                                sec.prop(settings,"grunge_color_tiling",text="Tiling",slider=True)

                            # HEIGHT
                            sec=eb.box()
                            mh=sec.row(align=True)
                            gc=mh.operator(
                                "material_lab.toggle_effect_group",
                                text="",
                                icon='TRIA_RIGHT' if _effect_group_collapsed(frame,"HEIGHT") else 'TRIA_DOWN',
                                emboss=False
                            )
                            gc.effect="Grunge"; gc.group="HEIGHT"
                            mh.label(text="HEIGHT",icon='MATERIAL')
                            if not _effect_group_collapsed(frame,"HEIGHT"):
                                sec.prop(settings,"grunge_height",text="Height",slider=True)

                        elif effect=="Dirt":
                            # MASK
                            sec=eb.box()
                            mh=sec.row(align=True)
                            gc=mh.operator(
                                "material_lab.toggle_effect_group",
                                text="",
                                icon='TRIA_RIGHT' if _effect_group_collapsed(frame,"MASK") else 'TRIA_DOWN',
                                emboss=False
                            )
                            gc.effect="Dirt"; gc.group="MASK"
                            mh.label(text="MASK",icon='MOD_NOISE')
                            vo=mh.operator("material_lab.view_mask",text="View Mask",icon='HIDE_ON' if settings.dirt_view_mask else 'HIDE_OFF')
                            vo.effect="Dirt"; vo.index=-1
                            if not _effect_group_collapsed(frame,"MASK"):
                                sec.prop(settings,"dirt_ao_amount",text="Amount",slider=True)
                                sec.prop(settings,"dirt_ao_depth",text="Depth",slider=True)
                                sec.prop(settings,"dirt_ao_distance",text="Distance",slider=True)
                                sec.prop(settings,"dirt_noise_scale",text="Noise",slider=True)
                                ramp=_find_effect_node(mat,effect,"MaskRamp")
                                if ramp:
                                    rr=sec.row(align=True)
                                    rr.prop(settings,"dirt_mask_ramp_collapsed",text="",icon='TRIA_RIGHT' if settings.dirt_mask_ramp_collapsed else 'TRIA_DOWN',emboss=False)
                                    rr.label(text="Mask Contrast")
                                    if not settings.dirt_mask_ramp_collapsed:
                                        sec.template_color_ramp(ramp,"color_ramp",expand=True)

                            # VISUAL
                            sec=eb.box()
                            mh=sec.row(align=True)
                            gc=mh.operator(
                                "material_lab.toggle_effect_group",
                                text="",
                                icon='TRIA_RIGHT' if _effect_group_collapsed(frame,"VISUAL") else 'TRIA_DOWN',
                                emboss=False
                            )
                            gc.effect="Dirt"; gc.group="VISUAL"
                            mh.label(text="VISUAL",icon='COLOR')
                            if not _effect_group_collapsed(frame,"VISUAL"):
                                sec.prop(settings,"dirt_color_source",text="Color")
                                if settings.dirt_color_source=="COLOR":
                                    color=_find_effect_node(mat,effect,"Color")
                                    if color: sec.prop(color.outputs["Color"],"default_value",text="Color")
                                else:
                                    r=sec.row(align=True)
                                    r.prop(settings,"dirt_color_image",text="Texture")
                                    op=r.operator("material_lab.open_effect_image",text="",icon='FILE_FOLDER'); op.effect="Dirt"; op.slot="COLOR"
                                    rnd=r.operator("material_lab.random_image",text="",icon='FILE_REFRESH'); rnd.effect="Dirt"; rnd.slot="COLOR"
                                    sec.prop(settings,"dirt_color_tiling",text="Tiling",slider=True)
                                sec.prop(settings,"dirt_amount",text="Intensity",slider=True)
                                sec.prop(settings,"dirt_color_blend",text="Blending",slider=True)
                                sec.prop(settings,"dirt_roughness",text="Roughness",slider=True)

                            # MAPPING
                            sec=eb.box()
                            mh=sec.row(align=True)
                            gc=mh.operator(
                                "material_lab.toggle_effect_group",
                                text="",
                                icon='TRIA_RIGHT' if _effect_group_collapsed(frame,"MAPPING") else 'TRIA_DOWN',
                                emboss=False
                            )
                            gc.effect="Dirt"; gc.group="MAPPING"
                            mh.label(text="MAPPING",icon='OBJECT_DATA')
                            if not _effect_group_collapsed(frame,"MAPPING"):
                                sec.prop(settings,"dirt_mapping_source",text="Coordinate")

                        elif effect=="Roughness":
                            # MASK
                            sec=eb.box()
                            mh=sec.row(align=True)
                            gc=mh.operator(
                                "material_lab.toggle_effect_group",
                                text="",
                                icon='TRIA_RIGHT' if _effect_group_collapsed(frame,"MASK") else 'TRIA_DOWN',
                                emboss=False
                            )
                            gc.effect="Roughness"; gc.group="MASK"
                            mh.label(text="MASK",icon='MOD_NOISE')
                            vo=mh.operator("material_lab.view_mask",text="View Mask",icon='HIDE_ON' if settings.roughness_view_mask else 'HIDE_OFF')
                            vo.effect="Roughness"; vo.index=-1
                            rr=mh.operator("material_lab.randomize_noise",text="Randomize",icon='FILE_REFRESH'); rr.effect="Roughness"
                            if not _effect_group_collapsed(frame,"MASK"):
                                r=sec.row(align=True)
                                r.prop(settings,"roughness_source",text="Source")
                                if settings.roughness_source=="IMAGE":
                                    r2=sec.row(align=True)
                                    r2.prop(settings,"roughness_image",text="Texture")
                                    op=r2.operator("material_lab.open_effect_image",text="",icon='FILE_FOLDER'); op.effect="Roughness"; op.slot="IMAGE"
                                elif settings.roughness_source=="NOISE":
                                    if not settings.roughness_noise_collapsed:
                                        for prop_name,label in (
                                            ("roughness_noise_scale","Scale"),
                                            ("roughness_detail","Detail"),
                                            ("roughness_noise_roughness","Roughness"),
                                            ("roughness_lacunarity","Lacunarity"),
                                            ("roughness_seed","Seed"),
                                            ("roughness_distortion","Distortion"),
                                        ):
                                            sec.prop(settings,prop_name,text=label,slider=True)
                                    nr=sec.row(align=True)
                                    nc=nr.operator("material_lab.toggle_noise_collapse",text="",icon='TRIA_RIGHT' if settings.roughness_noise_collapsed else 'TRIA_DOWN',emboss=False); nc.effect="Roughness"
                                    nr.label(text="Noise")
                                ramp=_find_effect_node(mat,effect,"Mask")
                                if ramp:
                                    rr=sec.row(align=True)
                                    rr.prop(settings,"roughness_mask_ramp_collapsed",text="",icon='TRIA_RIGHT' if settings.roughness_mask_ramp_collapsed else 'TRIA_DOWN',emboss=False)
                                    rr.label(text="Mask Contrast")
                                    if not settings.roughness_mask_ramp_collapsed:
                                        sec.template_color_ramp(ramp,"color_ramp",expand=True)
                                sec.prop(settings,"roughness_tiling",text="Tiling",slider=True)

                            # VISUAL
                            sec=eb.box()
                            mh=sec.row(align=True)
                            gc=mh.operator(
                                "material_lab.toggle_effect_group",
                                text="",
                                icon='TRIA_RIGHT' if _effect_group_collapsed(frame,"VISUAL") else 'TRIA_DOWN',
                                emboss=False
                            )
                            gc.effect="Roughness"; gc.group="VISUAL"
                            mh.label(text="VISUAL",icon='COLOR')
                            if not _effect_group_collapsed(frame,"VISUAL"):
                                sec.prop(settings,"roughness_value",text="Roughness",slider=True)

                            # MAPPING
                            sec=eb.box()
                            mh=sec.row(align=True)
                            gc=mh.operator(
                                "material_lab.toggle_effect_group",
                                text="",
                                icon='TRIA_RIGHT' if _effect_group_collapsed(frame,"MAPPING") else 'TRIA_DOWN',
                                emboss=False
                            )
                            gc.effect="Roughness"; gc.group="MAPPING"
                            mh.label(text="MAPPING",icon='OBJECT_DATA')
                            if not _effect_group_collapsed(frame,"MAPPING"):
                                sec.prop(settings,"roughness_mapping_source",text="Coordinate")

                        elif effect=="AO":
                            # MASK
                            sec=eb.box()
                            mh=sec.row(align=True)
                            gc=mh.operator(
                                "material_lab.toggle_effect_group",
                                text="",
                                icon='TRIA_RIGHT' if _effect_group_collapsed(frame,"MASK") else 'TRIA_DOWN',
                                emboss=False
                            )
                            gc.effect="AO"; gc.group="MASK"
                            mh.label(text="MASK",icon='MOD_NOISE')
                            vo=mh.operator("material_lab.view_mask",text="View Mask",icon='HIDE_ON' if settings.ao_view_mask else 'HIDE_OFF')
                            vo.effect="AO"; vo.index=-1
                            if not _effect_group_collapsed(frame,"MASK"):
                                ramp=_find_effect_node(mat,effect,"Ramp")
                                if ramp:
                                    rr=sec.row(align=True)
                                    rr.prop(settings,"ao_mask_ramp_collapsed",text="",icon='TRIA_RIGHT' if settings.ao_mask_ramp_collapsed else 'TRIA_DOWN',emboss=False)
                                    rr.label(text="Mask Contrast")
                                    if not settings.ao_mask_ramp_collapsed:
                                        sec.template_color_ramp(ramp,"color_ramp",expand=True)

                            # VISUAL
                            sec=eb.box()
                            mh=sec.row(align=True)
                            gc=mh.operator(
                                "material_lab.toggle_effect_group",
                                text="",
                                icon='TRIA_RIGHT' if _effect_group_collapsed(frame,"VISUAL") else 'TRIA_DOWN',
                                emboss=False
                            )
                            gc.effect="AO"; gc.group="VISUAL"
                            mh.label(text="VISUAL",icon='COLOR')
                            if not _effect_group_collapsed(frame,"VISUAL"):
                                color=_find_effect_node(mat,effect,"Color")
                                if color: sec.prop(color.outputs["Color"],"default_value",text="Color")
                                sec.prop(settings,"ao_amount",text="Amount",slider=True)
                                sec.prop(settings,"ao_depth",text="Depth",slider=True)
                                sec.prop(settings,"ao_distance",text="Distance",slider=True)
                                sec.prop(settings,"ao_color_blend",text="Blending",slider=True)

                        elif effect=="HSV":
                            # VISUAL
                            sec=eb.box()
                            mh=sec.row(align=True)
                            gc=mh.operator(
                                "material_lab.toggle_effect_group",
                                text="",
                                icon='TRIA_RIGHT' if _effect_group_collapsed(frame,"VISUAL") else 'TRIA_DOWN',
                                emboss=False
                            )
                            gc.effect="HSV"; gc.group="VISUAL"
                            mh.label(text="VISUAL",icon='COLOR')
                            if not _effect_group_collapsed(frame,"VISUAL"):
                                hsv=_find_effect_node(mat,effect,"HSV")
                                curves=_find_effect_node(mat,effect,"Curves")
                                if hsv:
                                    sec.prop(hsv.inputs["Hue"],"default_value",text="Hue",slider=True)
                                    sec.prop(hsv.inputs["Saturation"],"default_value",text="Saturation",slider=True)
                                    sec.prop(hsv.inputs["Value"],"default_value",text="Value",slider=True)
                                    sec.prop(hsv.inputs["Fac"],"default_value",text="Amount",slider=True)
                                if curves:
                                    cbox=sec.box()
                                    cbox.label(text="CURVES",icon='FCURVE')
                                    try:
                                        cbox.template_curve_mapping(curves,"mapping",type='COLOR')
                                    except Exception:
                                        cbox.label(text="Curves are available in the RGB Curves node.",icon='INFO')
        else:
            box.label(text="Select an object with a material.",icon='INFO')







# ------------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------------

classes = (
    ML_AddonPreferences,
    ML_MixerLayer,
    ML_MaterialSettings,
    ML_Settings,
    ML_OT_save_material,
    ML_OT_apply_material,
    ML_OT_duplicate_material,
    ML_OT_delete_material,
    ML_OT_choose_library_folder,
    ML_OT_save_material_asset,
    ML_OT_open_asset_browser,
    ML_OT_rename_material,
    ML_OT_assign_material,
    ML_OT_create_material,
    ML_UL_materials,
    ML_OT_set_active_slot,
    ML_OT_remove_slot,
    ML_OT_effect_texture_source,
    ML_OT_grunge_image_source,
    ML_OT_grunge_mapping_source,
    ML_OT_grunge_alpha_source,
    ML_OT_grunge_color_source,
    ML_OT_reset_material_tiling,
    ML_OT_toggle_collapse,
    ML_OT_toggle_effect_group,
    ML_OT_move_effect,
    ML_OT_view_mask,
    ML_OT_collapse_all_effects,
    ML_OT_toggle_look,
    ML_OT_reset_look,
    ML_OT_choose_source_folder,
    ML_OT_random_image,
    ML_OT_create_mixer_mask_texture,
    ML_OT_paint_mixer_mask_texture,
    ML_OT_open_effect_image,
    ML_OT_randomize_noise,
    ML_OT_reset_grunge_noise,
    ML_OT_reset_grunge_alpha_mask,
    ML_OT_toggle_noise_collapse,
    ML_OT_unwrap,
    ML_OT_add_look,
    ML_OT_remove_look,
    ML_OT_mixer_add_layer,
    ML_OT_mixer_create_bleed_target,
    ML_OT_mixer_remove_layer,
    ML_OT_mixer_move_layer,
    ML_OT_mixer_toggle_layer,
    ML_OT_mixer_build,
    ML_PT_main,
)



def _ml_sync_grunge_nodes_to_settings(_scene=None, _depsgraph=None):
    for mat in bpy.data.materials:
        try:
            if not mat.use_nodes or not mat.node_tree:
                continue
            if not any(n.name.startswith("ML_Grunge_") for n in mat.node_tree.nodes):
                continue
            settings=getattr(mat,"material_lab_settings",None)
            if settings:
                _sync_grunge_ui_from_nodes(mat,settings)
        except Exception:
            pass

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.material_lab_settings = PointerProperty(type=ML_Settings)
    bpy.types.Material.material_lab_settings = PointerProperty(type=ML_MaterialSettings)
    if _ml_sync_grunge_nodes_to_settings not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_ml_sync_grunge_nodes_to_settings)


def unregister():
    if _ml_sync_grunge_nodes_to_settings in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_ml_sync_grunge_nodes_to_settings)
    if hasattr(bpy.types.Material,"material_lab_settings"):
        del bpy.types.Material.material_lab_settings
    if hasattr(bpy.types.Scene,"material_lab_settings"):
        del bpy.types.Scene.material_lab_settings

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
