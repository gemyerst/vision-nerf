import os
import glob
import shutil
import configargparse
import tqdm
import imageio
import numpy as np
import torch
from torch.utils.data import Dataset
import torch.nn.functional as F
import torchvision.transforms as T
from PIL import Image, ImageEnhance
from rembg import remove
import cv2

from models.render_image import render_single_image
from models.model import VisionNerfModel
from models.sample_ray import RaySamplerSingleImage
from models.projection import Projector
from utils import img_HWC2CHW

def config_parser():
    parser = configargparse.ArgumentParser()
    # general
    parser.add_argument('--config', is_config_file=True, help='config file path')
    parser.add_argument('--expname', type=str, help='experiment name')
    parser.add_argument('--ckptdir', type=str, help='checkpoint folder')
    parser.add_argument('--ckpt_path', type=str, default='',
                        help='specific weights npy file to reload for coarse network')
    parser.add_argument('--outdir', type=str, help='output video directory')
    parser.add_argument("--local_rank", type=int, default=0, help='rank for distributed training')
    parser.add_argument("--include_src", action="store_true", help="Include source views in calculation")

    ########## dataset options ##########
    ## render dataset
    parser.add_argument('--data_path', type=str, help='the dataset to train')
    parser.add_argument('--data_type', type=str, default='srn', help='dataset type to use')
    parser.add_argument('--img_hw', type=int, nargs='+', help='image size option for dataset')
    parser.add_argument('--data_range', type=int,
                        default=[0, 50],
                        nargs='+',
                        help='data index to select from the dataset')
    parser.add_argument('--data_indices', type=int,
                        default=[0],
                        nargs='+',
                        help='data index to select from the dataset')
    parser.add_argument('--use_data_index', action='store_true',
                        help='use data_indices instead of data_range')
    parser.add_argument('--pose_index', type=int,
                        default=64,
                        help='source pose index to select from the dataset')
    parser.add_argument('--no_reload', action='store_true',
                        help='do not reload weights from saved ckpt (not used)')
    parser.add_argument('--distributed', action='store_true', help='if use distributed training (not used)')
    parser.add_argument('--skip', type=int,
                        default=1,
                        help='camera pose skip')

    ########## model options ##########
    ## ray sampling options
    parser.add_argument('--chunk_size', type=int, default=128,
                        help='number of rays processed in parallel, decrease if running out of memory')
    
    ## model options
    parser.add_argument('--im_feat_dim', type=int, default=128, help='image feature dimension')
    parser.add_argument('--mlp_feat_dim', type=int, default=512, help='mlp hidden dimension')
    parser.add_argument('--freq_num', type=int, default=10, help='how many frequency bases for positional encodings')
    parser.add_argument('--mlp_block_num', type=int, default=2, help='how many resnet blocks for coarse network')
    parser.add_argument('--coarse_only', action='store_true', help='use coarse network only')
    parser.add_argument("--anti_alias_pooling", type=int, default=1, help='if use anti-alias pooling')
    parser.add_argument('--num_source_views', type=int, default=1, help='number of views')
    parser.add_argument('--freeze_pos_embed', action='store_true', help='freeze positional embeddings')
    parser.add_argument('--no_skip_conv', action='store_true', help='disable skip convolution')

    ########### iterations & learning rate options (not used) ##########
    parser.add_argument('--lrate_feature', type=float, default=1e-3, help='learning rate for feature extractor')
    parser.add_argument('--lrate_mlp', type=float, default=5e-4, help='learning rate for mlp')
    parser.add_argument('--lrate_decay_factor', type=float, default=0.5,
                        help='decay learning rate by a factor every specified number of steps')
    parser.add_argument('--lrate_decay_steps', type=int, default=50000,
                        help='decay learning rate by a factor every specified number of steps')
    parser.add_argument('--warmup_steps', type=int, default=10000, help='num of iterations for warm-up')
    parser.add_argument('--scheduler', type=str, default='steplr', help='scheduler type to use [steplr]')
    parser.add_argument('--use_warmup', action='store_true', help='use warm-up scheduler')
    parser.add_argument('--bbox_steps', type=int, default=100000, help='iterations to use bbox sampling')

    ########## rendering options ##########
    parser.add_argument('--N_samples', type=int, default=64, help='number of coarse samples per ray')
    parser.add_argument('--N_importance', type=int, default=64, help='number of important samples per ray')
    parser.add_argument('--inv_uniform', action='store_true',
                        help='if True, will uniformly sample inverse depths')
    parser.add_argument('--det', action='store_true', help='deterministic sampling for coarse and fine samples')
    parser.add_argument('--white_bkgd', action='store_true',
                        help='apply the trick to avoid fitting to white background')

    return parser

def parse_intrinsic(path):
    with open(path, "r") as f:
        lines = f.readlines()
        focal, cx, cy, _ = map(float, lines[0].split())
    intrinsic = np.array([[focal, 0, cx, 0],
                          [0, focal, cy, 0],
                          [0,     0,  1, 0],
                          [0,     0,  0, 1]])
    return intrinsic

def parse_pose(path):
    return np.loadtxt(path, dtype=np.float32).reshape(4, 4)

def parse_pose_dvr(path, num_views):
    cameras = np.load(path)

    intrinsics = []
    c2w_mats = []

    for i in range(num_views):
        # ShapeNet
        wmat_inv_key = "world_mat_inv_" + str(i)
        wmat_key = "world_mat_" + str(i)
        kmat_key = "camera_mat_" + str(i)
        if wmat_inv_key in cameras:
            c2w_mat = cameras[wmat_inv_key]
        else:
            w2c_mat = cameras[wmat_key]
            if w2c_mat.shape[0] == 3:
                w2c_mat = np.vstack((w2c_mat, np.array([0, 0, 0, 1])))
            c2w_mat = np.linalg.inv(w2c_mat)

        intrinsics.append(cameras[kmat_key])
        c2w_mats.append(c2w_mat)

    intrinsics = np.stack(intrinsics, 0)
    c2w_mats = np.stack(c2w_mats, 0)

    return intrinsics, c2w_mats


class SRNRenderDataset(Dataset):
    """
    Dataset for rendering
    """
    def __init__(self, args, **kwargs):
        """
        Args:
            args.data_path: path to data directory
            args.img_hw: image size (resize if needed)
        """
        super().__init__()
        self.base_path = args.data_path
        self.dataset_name = os.path.basename(args.data_path)

        print("Loading ARCHI dataset", self.base_path, "name:", self.dataset_name)
        assert os.path.exists(self.base_path)

        intrinsic_paths = sorted(
            glob.glob(os.path.join(self.base_path, "*", "intrinsics.txt"))
        )

        self.intrinsics = []
        self.poses = []
        self.rgb_paths = []
        for path in tqdm.tqdm(intrinsic_paths):
            dir = os.path.dirname(path)
            curr_paths = sorted(glob.glob(os.path.join(dir, "pose", "*")))
            img_paths = sorted(glob.glob(os.path.join(dir, "*.png")))
            self.rgb_paths.append(img_paths)

            #pose_paths = [f.replace('rgb', 'pose').replace('png', 'txt') for f in curr_paths]
            c2w_mats = [parse_pose(x) for x in curr_paths]
            self.poses.append(c2w_mats)

            self.intrinsics.append(parse_intrinsic(path))

        self.rgb_paths = np.array(self.rgb_paths)
        self.poses = np.stack(self.poses, 0)
        self.intrinsics = np.array(self.intrinsics)

        assert(len(self.rgb_paths) == len(self.poses))

        self.define_transforms()
        self.img_hw = args.img_hw

        # default near/far plane depth

        self.z_near = 0.8
        self.z_far = 1.8

    def __len__(self):
        return len(self.intrinsics)

    def define_transforms(self):
        self.img_transforms = T.Compose(
            [T.ToTensor(), T.Normalize((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))]
        )
        self.mask_transforms = T.Compose(
            [T.ToTensor(), T.Normalize((0.0,), (1.0,))]
        )

    def __getitem__(self, index):
        # Read source RGB
        src_rgb_paths = self.rgb_paths[index]
        src_c2w_mats = self.poses[index]
        src_intrinsics = np.array([self.intrinsics[index]] * len(src_c2w_mats))

        src_rgbs = []
        src_masks = []
        for rgb_path in src_rgb_paths:
            img = imageio.imread(rgb_path)[..., :3]
            mask = (img.sum(axis=-1) != 255*3)[..., None].astype(np.uint8) * 255
            rgb = self.img_transforms(img)
            mask = self.mask_transforms(mask)

            h, w = rgb.shape[-2:]
            if (h != self.img_hw[0]) or (w != self.img_hw[1]):
                scale = self.img_hw[-1] / w
                src_intrinsics[:, :2] *= scale

                rgb = F.interpolate(rgb, size=self.img_hw, mode="area")
                mask = F.interpolate(mask, size=self.img_hw, mode="area")
            
            src_rgbs.append(rgb)
            src_masks.append(mask)

        depth_range = np.array([self.z_near, self.z_far])

        return {
            "rgb_path": rgb_path,
            "img_id": index,
            "img_hw": self.img_hw,
            "src_masks": torch.stack(src_masks).permute([0, 2, 3, 1]).float(),
            "src_rgbs": torch.stack(src_rgbs).permute([0, 2, 3, 1]).float(),
            "src_c2w_mats": torch.FloatTensor(src_c2w_mats),
            "src_intrinsics": torch.FloatTensor(src_intrinsics),
            "depth_range": torch.FloatTensor(depth_range)
        }

def trans_t(t):
    return torch.tensor(
        [[-1, 0, 0, 0], [0, 0, -1, t], [0, -1, 0, 0], [0, 0, 0, 1],], dtype=torch.float32,
    )

def rot_theta(angle):
    return torch.tensor(
        [
            [np.cos(angle), -np.sin(angle), 0, 0],
            [np.sin(angle), np.cos(angle), 0, 0],
            [0, 0, 1, 0],
            [0, 0, 0, 1],
        ],
        dtype=torch.float32,
    )

def rot_phi(phi):
    return torch.tensor(
        [
            [1, 0, 0, 0],
            [0, np.cos(phi), -np.sin(phi), 0],
            [0, np.sin(phi), np.cos(phi), 0],
            [0, 0, 0, 1],
        ],
        dtype=torch.float32,
    )

def pose_spherical(theta, phi, radius):
    """
    Spherical rendering poses, from NeRF
    """
    c2w = trans_t(radius)
    c2w = rot_phi(phi / 180.0 * np.pi) @ c2w
    c2w = rot_theta(theta / 180.0 * np.pi) @ c2w

    return c2w

def convertScale(img, alpha, beta):
    """Add bias and gain to an image with saturation arithmetics. Unlike
    cv2.convertScaleAbs, it does not take an absolute value, which would lead to
    nonsensical results (e.g., a pixel at 44 with alpha = 3 and beta = -210
    becomes 78 with OpenCV, when in fact it should become 0).
    """
    new_img = img * alpha + beta
    new_img[new_img < 0] = 0
    new_img[new_img > 255] = 255
    return new_img.astype(np.uint8)
# Automatic brightness and contrast optimization with optional histogram clipping
def automatic_brightness_and_contrast(image, clip_hist_percent=25):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Calculate grayscale histogram
    hist = cv2.calcHist([gray],[0],None,[256],[0,256])
    hist_size = len(hist)

    # Calculate cumulative distribution from the histogram
    accumulator = []
    accumulator.append(float(hist[0]))
    for index in range(1, hist_size):
        accumulator.append(accumulator[index -1] + float(hist[index]))

    # Locate points to clip
    maximum = accumulator[-1]
    clip_hist_percent *= (maximum/100.0)
    clip_hist_percent /= 2.0

    # Locate left cut
    minimum_gray = 0
    while accumulator[minimum_gray] < clip_hist_percent:
        minimum_gray += 1

    # Locate right cut
    maximum_gray = hist_size -1
    while accumulator[maximum_gray] >= (maximum - clip_hist_percent):
        maximum_gray -= 1

    # Calculate alpha and beta values
    alpha = 255 / (maximum_gray - minimum_gray)
    beta = -minimum_gray * alpha

    '''
    # Calculate new histogram with desired range and show histogram 
    new_hist = cv2.calcHist([gray],[0],None,[256],[minimum_gray,maximum_gray])
    plt.plot(hist)
    plt.plot(new_hist)
    plt.xlim([0,256])
    plt.show()
    '''

    auto_result = convertScale(image, alpha=alpha, beta=beta)
    return auto_result

def unsharp_mask(image, kernel_size=(5, 5), sigma=1.0, amount=1.0, threshold=0):
    """Return a sharpened version of the image, using an unsharp mask."""
    blurred = cv2.GaussianBlur(image, kernel_size, sigma)
    sharpened = float(amount + 1) * image - float(amount) * blurred
    sharpened = np.maximum(sharpened, np.zeros(sharpened.shape))
    sharpened = np.minimum(sharpened, 255 * np.ones(sharpened.shape))
    sharpened = sharpened.round().astype(np.uint8)
    if threshold > 0:
        low_contrast_mask = np.absolute(image - blurred) < threshold
        np.copyto(sharpened, image, where=low_contrast_mask)
    return sharpened

def remove_background(img_in):
    # resize image
    res = cv2.resize(img_in, dsize=(256, 256), interpolation=cv2.INTER_CUBIC)
    #img = Image.fromarray(res)
    #enhancer = ImageEnhance.Brightness(img)
    #enhanced = enhancer.enhance(.8)
    im = unsharp_mask(res)

    # convert to graky
    gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    mask = cv2.threshold(gray, 250, 255, cv2.THRESH_BINARY)[1]
    mask = 255 - mask # negate mask

    # apply morphology to remove isolated extraneous noise
    # use borderconstant of black since foreground touches the edges
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    # anti-alias the mask -- blur then stretch
    # blur alpha channel
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=2, sigmaY=2, borderType=cv2.BORDER_DEFAULT)

    # linear stretch so that 127.5 goes to 0, but 255 stays 255
    mask = (2 * (mask.astype(np.float32)) - 255.0).clip(0, 255).astype(np.uint8)

    # put mask into alpha channel
    result = res.copy()
    result = cv2.cvtColor(im, cv2.COLOR_BGR2BGRA)
    result[:, :, 3] = mask
    transparent_rgb = remove(result)
    return transparent_rgb


def make_transparent_png(img_in):
    # resize image
    res = cv2.resize(img_in, dsize=(256, 256), interpolation=cv2.INTER_CUBIC)

    white_pixels = np.where(
        (res[:, :, 0] > 215) &
        (res[:, :, 1] > 215) &
        (res[:, :, 2] > 215)
    )
    res[white_pixels] = [255, 255, 255]

    img = Image.fromarray(res)
    enhancer = ImageEnhance.Brightness(img)
    enhanced = enhancer.enhance(.85)

    transparent_rgb = remove(enhanced)

    #convert to transparent type
    img_rgba = transparent_rgb.convert("RGBA")
    pixel_data = img_rgba.getdata()

    new_data = []
    for data in pixel_data:
        if (data[3] >= 30):
            new_data.append((data[0], data[1], data[2], 255))
        else:
            new_data.append((data[0], data[1], data[2], 0))

    img_rgba.putdata(new_data)
    return img_rgba


def gen_eval(args):

    device = "cuda"
    print(f"checkpoints reload from {args.ckptdir}")

    dataset = SRNRenderDataset(args)

    # Create VisionNeRF model
    model = VisionNerfModel(args, False, False)
    # create projector
    projector = Projector(device=device)
    model.switch_to_eval()

    if args.use_data_index:
        data_index = args.data_indices
    else:
        data_index = np.arange(args.data_range[0], args.data_range[1])

    for d_idx in data_index:
        out_folder = os.path.join(args.outdir, args.expname, f'{d_idx:06d}')
        print(f'Rendering {dataset[d_idx]["rgb_path"][:-15]}')
        print(f'images will be saved to {out_folder}')
        os.makedirs(out_folder, exist_ok=True)

        obj_name = os.path.basename(dataset[d_idx]["rgb_path"][:-15])

        # save the args and config files
        f = os.path.join(out_folder, 'args.txt')
        with open(f, 'w') as file:
            for arg in sorted(vars(args)):
                attr = getattr(args, arg)
                file.write('{} = {}\n'.format(arg, attr))

        if args.config is not None:
            f = os.path.join(out_folder, 'config.txt')
            if not os.path.isfile(f):
                shutil.copy(args.config, f)

        sample = dataset[d_idx]
        pose_index = args.pose_index
        data_input = dict(
            rgb_path=sample['rgb_path'],
            img_id=sample['img_id'],
            img_hw=sample['img_hw'],
            tgt_intrinsic=sample['src_intrinsics'][0:1],
            src_masks=sample['src_masks'][pose_index][None, None, :],
            src_rgbs=sample['src_rgbs'][pose_index][None, None, :],
            src_c2w_mats=sample['src_c2w_mats'][pose_index][None, None, :],
            src_intrinsics=sample['src_intrinsics'][pose_index][None, None, :],
            depth_range=sample['depth_range'][None, :]
        )

        input_im = sample['src_rgbs'][pose_index].cpu().numpy() * 255.
        input_im = input_im.astype(np.uint8)
        filename = os.path.join(out_folder, 'input.png')
        imageio.imwrite(filename, input_im)

        render_poses = sample['src_c2w_mats']
        view_indices = np.arange(0, len(render_poses), args.skip)
        render_poses = render_poses[view_indices]

        imgs = []
        with torch.no_grad():

            for idx, pose in tqdm.tqdm(zip(view_indices, render_poses), total=len(view_indices)):
                if not args.include_src and idx == args.pose_index:
                    continue
                filename = os.path.join(out_folder, f'{idx:06}.png')
                data_input['tgt_c2w_mat'] = pose[None, :]

                # load training rays
                ray_sampler = RaySamplerSingleImage(data_input, device, render_stride=1)
                ray_batch = ray_sampler.get_all()
                featmaps = model.encode(ray_batch['src_rgbs'])

                ret = render_single_image(ray_sampler=ray_sampler,
                                          ray_batch=ray_batch,
                                          model=model,
                                          projector=projector,
                                          chunk_size=args.chunk_size,
                                          N_samples=args.N_samples,
                                          inv_uniform=args.inv_uniform,
                                          N_importance=args.N_importance,
                                          det=True,
                                          white_bkgd=args.white_bkgd,
                                          render_stride=1,
                                          featmaps=featmaps)
                    
                rgb_im = img_HWC2CHW(ret['outputs_fine']['rgb'].detach().cpu())
                # clamping RGB images
                rgb_im = torch.clamp(rgb_im, 0.0, 1.0)
                rgb_im = rgb_im.permute([1, 2, 0]).cpu().numpy()

                rgb_im = (rgb_im * 255.).astype(np.uint8)
                cleaned_rbg = make_transparent_png(rgb_im)
                imageio.imwrite(filename, cleaned_rbg)
                imgs.append(rgb_im)
                torch.cuda.empty_cache()

            imgs = np.stack(imgs, 0)
            imageio.mimsave(os.path.join(out_folder, f'output.gif'), imgs, fps=12)

if __name__ == '__main__':
    parser = config_parser()
    args = parser.parse_args()

    gen_eval(args)