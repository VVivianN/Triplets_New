import os
import random
import sys
from collections import OrderedDict

import math
import numpy as np
import torch
from accelerate import Accelerator
from einops.layers.torch import Rearrange
from timm.models.layers import trunc_normal_
from torch import nn


class MetricSaver(nn.Module):
    def __init__(self):
        super().__init__()
        self.best_acc = nn.Parameter(torch.zeros(1), requires_grad=False)

def load_model_dict(download_path, save_path=None, check_hash=True) -> OrderedDict:
    if download_path.startswith('http'):
        state_dict = torch.hub.load_state_dict_from_url(download_path, model_dir=save_path, check_hash=check_hash, map_location=torch.device('cpu'))
    else:
        state_dict = torch.load(download_path, map_location=torch.device('cpu'))
    return state_dict


def resume_train_state(model, checkpoint, optimizers, schedulers, accelerator):
    try:
        base_path = f"{os.getcwd()}/model_store/{checkpoint}/checkpoint"
        epoch_checkpoint = torch.load(base_path + "/epoch.pth.tar", map_location=accelerator.device)
        best_score = epoch_checkpoint['best_score']
        best_metrics = epoch_checkpoint['best_metrics']
        starting_epoch = epoch_checkpoint['epoch'] + 1
        train_step = epoch_checkpoint['train_step']
        val_step = epoch_checkpoint['val_step']
        model = load_pretrain_model(base_path + "/pytorch_model.bin", model, accelerator)
        if isinstance(optimizers, list):
            optimizers = load_param(base_path, optimizers, accelerator, 'optimizer')
            schedulers = load_param(base_path, schedulers, accelerator, 'scheduler')
        else:
            optimizers.load_state_dict(torch.load(base_path + "/optimizer.bin"))
            schedulers.load_state_dict(torch.load(base_path + "/scheduler.bin"))
        
        accelerator.print(f'Loading training state successfully! Start training from {starting_epoch}, Best score: {best_score}')
        
        return model, optimizers, schedulers, starting_epoch, train_step, val_step, best_score, best_metrics
    except Exception as e:
        accelerator.print(e)
        accelerator.print(f'Failed to load training state!')
        return model, optimizers, schedulers, 0, 0, 0, torch.nn.Parameter(torch.tensor([0.0]), requires_grad=False), {}


def load_pretrain_model(pretrain_path: str, model: nn.Module, accelerator: Accelerator):
    try:
        state_dict = load_model_dict(pretrain_path)
        model.load_state_dict(state_dict)
        accelerator.print(f'Successfully loaded the training model！')
        return model
    except Exception as e:
        accelerator.print(e)
        accelerator.print(f'Failed to load the training model！')
        return model



    """
    把输入图片变成patch, 图片形状为立方体
    imgs: (N, modality, H, W, D)
    x: (N, L, patch_size_hw**2 * patch_size_depth) [batch_size, num_patches, 每个patch大小]
    """

    imgs = Rearrange('b c (h p1) (w p2) (d p3)-> b (h w d) (p1 p2 p3 c)', p1=path_size, p2=path_size, p3=path_size)(imgs)
    # x = imgs.reshape(shape=(imgs.shape[0], modality, h, path_size, w, path_size, d, path_size))  # [N, modality, patch_h, patch_size, patch_w, patch_size, patch_d, patch_size]
    # x = torch.einsum('nmhowpdq->nhwdopqm', x)  # [N, patch_h, patch_w, patch_d, patch_size, patch_size, patch_size, modality]
    # imgs = x.reshape(shape=(x.shape[0], num_patches, path_size ** 3 * modality))  # [N, num_patches, pixel_patches]
    return imgs



def same_seeds(seed):
    torch.multiprocessing.set_sharing_strategy('file_system')
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    torch.backends.cudnn.benchmark = True
    # torch.backends.cudnn.deterministic = True



def init_weights(m):
    if isinstance(m, nn.Linear):
        trunc_normal_(m.weight, std=0.02)
        if isinstance(m, nn.Linear) and m.bias is not None:
            nn.init.constant_(m.bias, 0)
    elif isinstance(m, nn.LayerNorm):
        nn.init.constant_(m.bias, 0)
        nn.init.constant_(m.weight, 1.0)




class Logger(object):
    def __init__(self, logdir: str):
        self.console = sys.stdout
        if logdir is not None:
            os.makedirs(logdir)
            self.log_file = open(logdir + '/log.txt', 'w')
        else:
            self.log_file = None
        sys.stdout = self
        sys.stderr = self

    def __del__(self):
        self.close()

    def __enter__(self):
        pass

    def __exit__(self, *args):
        self.close()

    def write(self, msg):
        self.console.write(msg)
        if self.log_file is not None:
            self.log_file.write(msg)

    def flush(self):
        self.console.flush()
        if self.log_file is not None:
            self.log_file.flush()
            os.fsync(self.log_file.fileno())

    def close(self):
        self.console.close()
        if self.log_file is not None:
            self.log_file.close()


def get_params_groups(model):
    regularized = []
    not_regularized = []
    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue
        # we do not regularize biases nor Norm parameters
        if name.endswith(".bias") or len(param.shape) == 1:
            not_regularized.append(param)
        else:
            regularized.append(param)
    return [{'params': regularized}, {'params': not_regularized, 'weight_decay': 0.}]


def get_world_size(accelerator):
    return accelerator.num_processes

def get_weight_balancing(config):
    dataset_choose = config.trainer.dataset
    if dataset_choose == 'T45':
        config = config.dataset.T45
    elif dataset_choose == 'T50':
        config = config.dataset.T50
    # 50:   cholecT50, data splits as used in rendezvous paper
    # 50ch: cholecT50, data splits as used in CholecTriplet challenge
    # 45cv: cholecT45, official data splits (cross-val)
    # 50cv: cholecT50, official data splits (cross-val)
    case = config.dataset_variant
    switcher = {
        'cholect50': {
            'tool'  :   [0.08084519, 0.81435289, 0.10459284, 2.55976864, 1.630372490, 1.29528455],
            'verb'  :   [0.31956735, 0.07252306, 0.08111481, 0.81137309, 1.302895320, 2.12264151, 1.54109589, 8.86363636, 12.13692946, 0.40462028],
            'target':   [0.06246232, 1.00000000, 0.34266478, 0.84750219, 14.80102041, 8.73795181, 1.52845100, 5.74455446, 0.285756500, 12.72368421, 0.6250808,  3.85771277, 6.95683453, 0.84923888, 0.40130032]
        },
        'cholect50-challenge': {
            'tool':     [0.08495163, 0.88782288, 0.11259564, 2.61948830, 1.784866470, 1.144624170],
            'verb':     [0.39862805, 0.06981640, 0.08332925, 0.81876204, 1.415868390, 2.269359150, 1.28428410, 7.35822511, 18.67857143, 0.45704490],
            'target':   [0.07333818, 0.87139287, 0.42853950, 1.00000000, 17.67281106, 13.94545455, 1.44880997, 6.04889590, 0.326188650, 16.82017544, 0.63577586, 6.79964539, 6.19547658, 0.96284208, 0.51559559]
        },
        'cholect45-crossval': {
            1: {
                'tool':     [0.08165644, 0.91226868, 0.10674758, 2.85418156, 1.60554885, 1.10640067],
                'verb':     [0.37870137, 0.06836869, 0.07931255, 0.84780024, 1.21880342, 2.52836879, 1.30765704, 6.88888889, 17.07784431, 0.45241117],
                'target':   [0.07149629, 1.0, 0.41013597, 0.90458015, 13.06299213, 12.06545455, 1.5213205, 5.04255319, 0.35808332, 45.45205479, 0.67493897, 7.04458599, 9.14049587, 0.97330595, 0.52633249]
                },
            2: {
                'tool':     [0.0854156, 0.89535362, 0.10995253, 2.74936869, 1.78264429, 1.13234529],
                'verb':     [0.36346863, 0.06771776, 0.07893261, 0.82842725, 1.33892161, 2.13049748, 1.26120359, 5.72674419, 19.7, 0.43189126],
                'target':   [0.07530655, 0.97961957, 0.4325135, 0.99393438, 15.5387931, 14.5951417, 1.53862569, 6.01836394, 0.35184462, 15.81140351, 0.709506, 5.79581994, 8.08295964, 1.0, 0.52689272]
            },
            3: {
                "tool" :   [0.0915228, 0.89714969, 0.12057004, 2.72128174, 1.94092281, 1.12948557],
                "verb" :   [0.43636862, 0.07558554, 0.0891017, 0.81820519, 1.53645582, 2.31924198, 1.28565657, 6.49387755, 18.28735632, 0.48676763],
                "target" : [0.06841828, 0.90980736, 0.38826607, 1.0, 14.3640553, 12.9875, 1.25939394, 5.38341969, 0.29060227, 13.67105263, 0.59168565, 6.58985201, 5.72977941, 0.86824513, 0.47682423]

            },
            4: {
                'tool':     [0.08222218, 0.85414117, 0.10948695, 2.50868784, 1.63235867, 1.20593318],
                'verb':     [0.41154261, 0.0692142, 0.08427214, 0.79895288, 1.33625219, 2.2624166, 1.35343681, 7.63, 17.84795322, 0.43970609],
                'target':   [0.07536126, 0.85398445, 0.4085784, 0.95464422, 15.90497738, 18.5978836, 1.55875831, 5.52672956, 0.33700863, 15.41666667, 0.74755423, 5.4921875, 6.11304348, 1.0, 0.50641118],
            },
            5: {
                'tool':     [0.0804654, 0.92271157, 0.10489631, 2.52302243, 1.60074906, 1.09141982],
                'verb':     [0.50710436, 0.06590258, 0.07981184, 0.81538866, 1.29267277, 2.20525568, 1.29699248, 7.32311321, 25.45081967, 0.46733895],
                'target':   [0.07119395, 0.87450495, 0.43043372, 0.86465981, 14.01984127, 23.7114094, 1.47577277, 5.81085526, 0.32129865, 22.79354839, 0.63304067, 6.92745098, 5.88833333, 1.0, 0.53175798]
            }
        },
        'cholect50-crossval': {
            1:{
                'tool':     [0.0828851, 0.8876, 0.10830995, 2.93907285, 1.63884786, 1.14499484],
                'verb':     [0.29628942, 0.07366916, 0.08267971, 0.83155428, 1.25402434, 2.38358209, 1.34938741, 7.56872038, 12.98373984, 0.41502079],
                'target':   [0.06551745, 1.0, 0.36345711, 0.82434783, 13.06299213, 8.61818182, 1.4017744, 4.62116992, 0.32822238, 45.45205479, 0.67343211, 4.13200498, 8.23325062, 0.88527215, 0.43113306],

            },
            2:{
                'tool':     [0.08586283, 0.87716737, 0.11068887, 2.84210526, 1.81016949, 1.16283571],
                'verb':     [0.30072757, 0.07275414, 0.08350168, 0.80694143, 1.39209979, 2.22754491, 1.31448763, 6.38931298, 13.89211618, 0.39397505],
                'target':   [0.07056703, 1.0, 0.39451115, 0.91977006, 15.86206897, 9.68421053, 1.44483706, 5.44378698, 0.31858714, 16.14035088, 0.7238395, 4.20571429, 7.98264642, 0.91360477, 0.43304307],
            },
            3:{
            'tool':      [0.09225068, 0.87856006, 0.12195811, 2.82669323, 1.97710987, 1.1603972],
                'verb':     [0.34285159, 0.08049804, 0.0928239, 0.80685714, 1.56125608, 2.23984772, 1.31471136, 7.08835341, 12.17241379, 0.43180428],
                'target':   [0.06919395, 1.0, 0.37532866, 0.9830703, 15.78801843, 8.99212598, 1.27597765, 5.36990596, 0.29177312, 15.02631579, 0.64935557, 5.08308605, 5.86643836, 0.86580743, 0.41908257], 
            },
            4:{
                'tool':     [0.08247885, 0.83095539, 0.11050268, 2.58193042, 1.64497676, 1.25538881],
                'verb':     [0.31890981, 0.07380354, 0.08804592, 0.79094077, 1.35928144, 2.17017208, 1.42947103, 8.34558824, 13.19767442, 0.40666428],
                'target':   [0.07777646, 0.95894072, 0.41993829, 0.95592153, 17.85972851, 12.49050633, 1.65701092, 5.74526929, 0.33763901, 17.31140351, 0.83747083, 3.95490982, 6.57833333, 1.0, 0.47139615],
            },
            5:{
                'tool':     [0.07891691, 0.89878025, 0.10267677, 2.53805556, 1.60636428, 1.12691169],
                'verb':     [0.36420961, 0.06825313, 0.08060635, 0.80956984, 1.30757221, 2.09375, 1.33625848, 7.9009434, 14.1350211, 0.41429631],
                'target':   [0.07300329, 0.97128713, 0.42084942, 0.8829883, 15.57142857, 19.42574257, 1.56521739, 5.86547085, 0.32732733, 25.31612903, 0.70171674, 4.55220418, 6.13125, 1.0, 0.48528321],
            }
        }
    }
    if 'crossval' in config.dataset_variant:
        tool_weight   = switcher.get(case)[config.kfold]['tool']
        verb_weight   = switcher.get(case)[config.kfold]['verb']
        target_weight = switcher.get(case)[config.kfold]['target']
    else:
        tool_weight   = switcher.get(case)['tool']
        verb_weight   = switcher.get(case)['verb']
        target_weight = switcher.get(case)['target']
    if config.weight_randa == True:
        tool_weight     = [0.93487068, 0.94234964, 0.93487068, 1.18448115, 1.02368339, 0.97974447]
        verb_weight     = [0.60002400, 0.60002400, 0.60002400, 0.61682467, 0.67082683, 0.80163207, 0.70562823, 2.11208448, 2.69230769, 0.60062402]
        target_weight   = [0.49752894, 0.52041527, 0.49752894, 0.51394739, 2.71899565, 1.75577963, 0.58509403, 1.25228034, 0.49752894, 2.42993134, 0.49802647, 0.87266576, 1.36074165, 0.50150917, 0.49802647]

    return tool_weight, verb_weight, target_weight

def load_param(base_path, param_dict, accelerator, type='optimizer'):
    num = 0
    for key in param_dict.keys():
        if num == 0:
            add = ''
        else:
            add = f'_{num}'
        param_dict[key].load_state_dict(torch.load(base_path + f"/{type}{add}.bin", map_location=accelerator.device))  
        num += 1
    return param_dict
        

def set_param_in_device(accelerator, param_dict):
    new_dict = {}
    for key in param_dict.keys():
        new_dict[key] = accelerator.prepare(param_dict[key])
    return new_dict

def step_params(param_dict):
    for key in param_dict.keys():
        param_dict[key].step()

def add_tokens_tokenizer(tokenizer, all_list):
    add = []
    for word in all_list:
        if word in tokenizer.vocab:
            pass
        else:
            print(f"'{word}' is not in the BERT vocabulary.")
            add.append(word)
    num_added_toks = tokenizer.add_tokens(add)
    print('Now we have added', num_added_toks, 'tokens')
    return tokenizer

