import os
import json
from typing import Optional, List, Union, Tuple

import pprint
from tqdm import tqdm
from PIL import ImageOps
from PIL import Image as im
from pxr import Gf, Sdf, Vt
from pxr import Usd, UsdGeom
from termcolor import colored
from scipy.spatial.transform import Rotation as R

import mujoco
from mujoco import mjtGeom
from mujoco.usd.usd_utils import *
from mujoco.usd.usd_component import *
from mujoco import mjv_averageCamera
from mujoco import _structs, _constants, _enums


class USDExporter:

    def __init__(
        self,
        model: _structs.MjModel,
        height: int = 480,
        width: int = 480,
        max_geom: int = 10000,
        output_directory_name: str = "mujoco_usdpkg",
        output_directory_root: str = "./",
        light_intensity: int = 10000,
        camera_names: List[str] = None,
        specialized_materials_file: str = None,
        verbose: bool = True,
    ):
        """ Initializes a new USD Renderer
        Args:
            model: an MjModel instance.
            height: image height in pixels.
            width: image width in pixels.
            max_geom: Optional integer specifying the maximum number of geoms that can
                be rendered in the same scene. If None this will be chosen automatically
                based on the estimated maximum number of renderable geoms in the model.
            output_directory_name: name of root directory to store outputted frames and assets generated by the USD renderer.
            output_directory_root: path to root directory storing generated frames and assets by the USD renderer.
            verbose: decides whether to print updates.
        """

        buffer_width = model.vis.global_.offwidth
        buffer_height = model.vis.global_.offheight

        if width > buffer_width:
            raise ValueError(f"""
                Image width {width} > framebuffer width {buffer_width}. Either reduce the image
                width or specify a larger offscreen framebuffer in the model XML using the
                clause:
                <visual>
                <global offwidth="my_width"/>
                </visual>""".lstrip())

        if height > buffer_height:
            raise ValueError(f"""
                Image height {height} > framebuffer height {buffer_height}. Either reduce the
                image height or specify a larger offscreen framebuffer in the model XML using
                the clause:
                <visual>
                <global offheight="my_height"/>
                </visual>""".lstrip())

        self.model = model
        self.height = height
        self.width = width
        self.max_geom = max_geom
        self.output_directory_name = output_directory_name
        self.output_directory_root = output_directory_root
        self.light_intensity = light_intensity
        self.camera_names = camera_names
        self.specialized_materials_file = specialized_materials_file
        self.verbose = verbose

        # assert specialized_materials_file.endswith('.json')
        # self.specialized_materials = json.loads(specialized_materials_file)

        self.frame_count = 0 # maintains how many times we have saved the scene
        self.updates = 0

        self.geom_name2usd = {}

        # initializing rendering requirements
        self.renderer = mujoco.Renderer(model, height, width, max_geom)
        self._initialize_usd_stage()
        self._scene_option = _structs.MjvOption() # using default scene option

        # initializing output_directories
        self._initialize_output_directories()

        # loading required textures for the scene
        self._load_textures()

        self.extra_added_water = False
        self.extra_added_stove = False
        self.extra_added_coffee = False

    @property
    def usd(self):
        return self.stage.GetRootLayer().ExportToString()
    
    @property
    def scene(self):
        return self.renderer.scene        

    def _initialize_usd_stage(self):
        self.stage = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageUpAxis(self.stage, UsdGeom.Tokens.z)
        self.stage.SetStartTimeCode(0)
        # add as user imput
        self.stage.SetTimeCodesPerSecond(24.0)

        default_prim = UsdGeom.Xform.Define(self.stage, Sdf.Path("/World")).GetPrim()
        self.stage.SetDefaultPrim(default_prim)

    def _initialize_output_directories(self):
        self.output_directory_path = os.path.join(self.output_directory_root, self.output_directory_name)
        if not os.path.exists(self.output_directory_path):
            os.makedirs(self.output_directory_path)

        self.frames_directory = os.path.join(self.output_directory_path, "frames")
        if not os.path.exists(self.frames_directory):
            os.makedirs(self.frames_directory)
        
        self.assets_directory = os.path.join(self.output_directory_path, "assets")
        if not os.path.exists(self.assets_directory):
            os.makedirs(self.assets_directory)

        if self.verbose:
            print(colored(f"Writing output frames and assets to {self.output_directory_path}", "green"))

    def update_scene(
        self,
        data: _structs.MjData,
        scene_option: Optional[_structs.MjvOption] = None,
    ):
        """ Updates the scene with latest sim data
        Args:
            data: structure storing current simulation state
            scene_option: we use this to determine which geom groups to activate
        """

        self.frame_count += 1

        scene_option = scene_option or self._scene_option

        # update the mujoco renderer
        self.renderer.update_scene(data, 
                                   scene_option=scene_option)

        # TODO: update scene options
        if self.updates == 0:
            self._initialize_usd_stage()

            self._load_lights()
            self._load_cameras()

        self._update_geoms()
        self._update_lights()
        self._update_cameras(data, scene_option=scene_option)

        self.updates += 1

    def _load_textures(self):
        # TODO: remove code once added internally to mujoco
        data_adr = 0
        self.texture_files = []
        for texture_id in tqdm(range(self.model.ntex)):
            texture_height = self.model.tex_height[texture_id]
            texture_width = self.model.tex_width[texture_id]
            pixels = 3*texture_height*texture_width
            img = im.fromarray(self.model.tex_rgb[data_adr:data_adr+pixels].reshape(texture_height, texture_width, 3))
            img = ImageOps.flip(img)

            texture_file_name = f"texture_{texture_id}.png"

            img.save(os.path.join(self.assets_directory, texture_file_name))

            relative_path = os.path.relpath(self.assets_directory, self.frames_directory)
            img_path = os.path.join(relative_path, texture_file_name) # relative path, TODO: switch back to this

            self.texture_files.append(img_path)

            data_adr += pixels

        if self.verbose:
            print(colored(f"Completed writing {self.model.ntex} textures to {self.assets_directory}", "green"))

    def _load_geom(
        self,
        geom: _structs.MjvGeom
    ):
                
        geom_name = mujoco.mj_id2name(self.model, geom.objtype, geom.objid)
        assert geom_name not in self.geom_name2usd

        # handles meshes in scene
        if geom.type == mjtGeom.mjGEOM_MESH:
            usd_geom = USDMesh(stage=self.stage,
                                model=self.model,
                                geom=geom,
                                objid=geom_name,
                                dataid=self.model.geom_dataid[geom.objid],
                                rgba=geom.rgba,
                                texture_file=self.texture_files[geom.texid] if geom.texid != -1 else None)
        elif geom.type == mjtGeom.mjGEOM_PLANE:
            usd_geom = USDPlaneMesh(stage=self.stage,
                                    geom=geom,
                                    objid=geom_name,
                                    rgba=geom.rgba,
                                    texture_file=self.texture_files[geom.texid] if geom.texid != -1 else None)
        elif geom.type == mjtGeom.mjGEOM_SPHERE:
            usd_geom = USDSphereMesh(stage=self.stage,
                                        geom=geom,
                                        objid=geom_name,
                                        rgba=geom.rgba,
                                        texture_file=self.texture_files[geom.texid] if geom.texid != -1 else None)
        elif geom.type == mjtGeom.mjGEOM_CAPSULE:
            usd_geom = USDCapsule(stage=self.stage,
                                    geom=geom,
                                    objid=geom_name,
                                    rgba=geom.rgba,
                                    texture_file=self.texture_files[geom.texid] if geom.texid != -1 else None)
        elif geom.type == mjtGeom.mjGEOM_ELLIPSOID:
            usd_geom = USDEllipsoid(stage=self.stage,
                                    geom=geom,
                                    objid=geom_name,
                                    rgba=geom.rgba,
                                    texture_file=self.texture_files[geom.texid] if geom.texid != -1 else None)
        elif geom.type == mjtGeom.mjGEOM_CYLINDER:
            usd_geom = USDCylinderMesh(stage=self.stage,
                                        geom=geom,
                                        objid=geom_name,
                                        rgba=geom.rgba,
                                        texture_file=self.texture_files[geom.texid] if geom.texid != -1 else None)
        elif geom.type == mjtGeom.mjGEOM_BOX:
            usd_geom = USDCubeMesh(stage=self.stage,
                                    geom=geom,
                                    objid=geom_name,
                                    rgba=geom.rgba,
                                    texture_file=self.texture_files[geom.texid] if geom.texid != -1 else None)
        else:
            usd_geom = None

        self.geom_name2usd[geom_name] = usd_geom

    def _update_geoms(self):

        geom_names = set(self.geom_name2usd.keys())

        # iterate through all geoms in the scene and makes update
        for i in range(self.scene.ngeom):
            geom = self.scene.geoms[i]
            geom_name = mujoco.mj_id2name(self.model, geom.objtype, geom.objid)

            if geom_name not in self.geom_name2usd:
                self._load_geom(geom)
                if self.geom_name2usd[geom_name]:
                    self.geom_name2usd[geom_name].update_visibility(False, 0)

            if self.geom_name2usd[geom_name]:
                self.geom_name2usd[geom_name].update(pos=geom.pos,
                                                     mat=geom.mat,
                                                     visible=geom.rgba[3] > 0,
                                                     frame=self.updates)
            if geom_name in geom_names: 
                geom_names.remove(geom_name)

        for geom_name in geom_names:
            if self.geom_name2usd[geom_name]:
                self.geom_name2usd[geom_name].update_visibility(False, self.updates)
                    
    def _load_lights(self):
        # initializes an usd light object for every light in the scene
        self.usd_lights = []
        for i in range(self.scene.nlight):
            light = self.scene.lights[i]
            if not np.allclose(light.pos, [0, 0, 0]):
                self.usd_lights.append(USDSphereLight(stage=self.stage,
                                                    objid=i))
            else:
                self.usd_lights.append(None)
                        
    def _update_lights(self):
        for i in range(self.scene.nlight):
            light = self.scene.lights[i]
            if not np.allclose(light.pos, [0, 0, 0]):
                self.usd_lights[i].update(pos=light.pos,
                                          intensity=self.light_intensity,
                                          color=light.diffuse,
                                          frame=self.updates)
                
        print("done updating")
            
    def _load_cameras(self):
        self.usd_cameras = []
        for name in self.camera_names:            
            self.usd_cameras.append(USDCamera(stage=self.stage,
                                    objid=name))
        
    def _update_cameras(
        self,
        data: _structs.MjData,
        scene_option: Optional[_structs.MjvOption] = None
    ):
        for i in range(len(self.usd_cameras)):

            camera = self.usd_cameras[i]
            camera_name = self.camera_names[i]

            self.renderer.update_scene(data, 
                                        scene_option=scene_option,
                                        camera=camera_name)
            
            avg_camera = mjv_averageCamera(self.scene.camera[0], self.scene.camera[1])

            forward = avg_camera.forward
            up = avg_camera.up
            right = np.cross(forward, up)

            R = np.eye(3)
            R[:, 0] = right
            R[:, 1] = up
            R[:, 2] = -forward

            camera.update(cam_pos=avg_camera.pos,
                          cam_mat=R,
                          frame=self.updates)
        
    def add_light(
        self,
        pos: List[float],
        intensity:int,
        radius: Optional[float] = 1.0,
        color: Optional[np.array] = np.array([0.3, 0.3, 0.3]),
        objid: Optional[int]=1,
        light_type: Optional[str]="sphere"
    ):
        
        if light_type == "sphere": 
            new_light = USDSphereLight(stage=self.stage,
                                       objid=objid,
                                       radius=radius)

            new_light.update(pos=pos,
                             intensity=intensity,
                             color=color,
                             frame=0)
        elif light_type == "dome":
            new_light = USDDomeLight(stage=self.stage,
                                     objid=objid)

            new_light.update(intensity=intensity,
                             color=color,
                             frame=0)
        
    def add_camera(
        self, 
        pos:List[float], 
        rotation_xyz:List[float],
        objid: Optional[int]=1
    ):
        new_camera = USDCamera(stage=self.stage,
                               objid=objid)
        
        r = R.from_euler('xyz', rotation_xyz, degrees=True)
        new_camera.update(cam_pos=pos,
                          cam_mat=r.as_matrix(),
                          frame=0)
        
    def save_scene(
        self,
        filetype: str = "usd"
    ):
        self.stage.SetEndTimeCode(self.frame_count)
        self.stage.Export(f'{self.output_directory_root}/{self.output_directory_name}/frames/frame_{self.frame_count}_.{filetype}')
        if self.verbose:
            print(colored(f"Writing frame_{self.frame_count}", "green"))