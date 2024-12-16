import os
import pdb
import torch
import numpy as np
import torch.nn as nn
from torch.optim import AdamW
from torchvision.transforms import Compose
from torch.optim.lr_scheduler import MultiStepLR
from torch.utils.data.distributed import DistributedSampler
from bemapnet.models.network import BeMapNet
from bemapnet.engine.core import BeMapNetCli
from bemapnet.engine.experiment import BaseExp
from bemapnet.dataset.av2 import Argoverse2MapDataset
from bemapnet.dataset.transform import Normalize, ToTensor
from bemapnet.utils.misc import get_param_groups, is_distributed


class EXPConfig:

    CLASS_NAMES = ["lane_divider", "ped_crossing", "drivable_area"]
    IMAGE_SHAPE = (1550, 2048)
    ida_conf = dict(resize_dims=(896, 512), up_crop_ratio=0.25, rand_flip=True, rot_lim=False)
    INPUT_SHAPE = [int(ida_conf["resize_dims"][1] * (1 - ida_conf["up_crop_ratio"])), int(ida_conf["resize_dims"][0])]
    point_cloud_range = [-30.0, -15.0, -5.0, 30.0, 15.0, 3.0]
    bev_h_ = 100
    bev_w_ = 200
    _dim_ = 256

    map_conf = dict(
        dataset_name="argoverse2",
        av2_root="data/argoverse2",
        anno_root="data/argoverse2/geo_interval_1",
        split_dir="assets/splits/argoverse2",
        num_classes=3,
        ego_size=(60, 30),
        map_region=(30, 30, 15, 15),
        map_resolution=0.15,
        map_size=(400, 200),
        mask_key="instance_mask8",
        line_width=8,
        save_thickness=1,
    )

    bezier_conf = dict(
        num_degree=(2, 1, 3),
        max_pieces=(3, 1, 7),
        num_points=(7, 2, 22),
        piece_length=100,
        max_instances=40,
    )

    dataset_setup = dict(
        img_key_list=['ring_front_center', 'ring_front_right', 'ring_front_left', 'ring_rear_right', 'ring_rear_left',
                      'ring_side_right', 'ring_side_left'],
        img_norm_cfg=dict(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225], to_rgb=True),
    )

    global_map_cfg = dict(
        pc_range=point_cloud_range,
        bev_h=bev_h_,
        bev_w=bev_w_,
        fuse_method='prob',  # all or prob    train: all   test:prob
        raster_size=[0.30, 0.30],
        dataset='av2',
        load_map_path=None,
        save_map_path=None,
        update_map=True,
    )
    use_local_map = True
    raster_cfg = dict(
        bev_h=bev_h_,
        bev_w=bev_w_,
    )

    model_setup = dict(
        use_depth_loss=False,
        global_map_cfg=global_map_cfg,
        raster_cfg=raster_cfg,
        im_backbone=dict(
            arch_name="resnet",
            bkb_kwargs=dict(
                depth=50,
                num_stages=4,
                out_indices=(0, 1, 2, 3),
                frozen_stages=1,
                norm_cfg=dict(type='SyncBN', requires_grad=True),
                norm_eval=True,
                style='pytorch',
                init_cfg=dict(
                    type='Pretrained',
                    checkpoint='assets/weights/resnet50-0676ba61.pth'),           # from pytorch
                with_cp=True,
            ),
            ret_layers=3,
            fpn_kwargs=dict(
                conv_channels=(512, 1024, 2048),
                fpn_cell_repeat=3,
                fpn_num_filters=120,  # 128 -> 120  avoid OOM only
                norm_layer=nn.SyncBatchNorm,
                use_checkpoint=True,
                tgt_shape=(21, 49)
            ),
        ),
        bev_decoder=dict(
            arch_name="transformer",
            net_kwargs=dict(
                key='im_nek_features',
                in_channels=600,
                num_camera=7,
                src_shape=(21, 49*7),
                query_shape=(64, 32),
                d_model=512,
                nhead=8,
                num_encoder_layers=2,
                num_decoder_layers=4,
                dim_feedforward=1024,
                src_pos_embed='ipm_learned',
                tgt_pos_embed='ipm_learned',
                dropout=0.1,
                activation="relu",
                normalize_before=False,
                return_intermediate_dec=True,
                use_checkpoint=True,
                ipm_proj_conf=dict(
                    map_size=map_conf["map_size"],
                    map_resolution=map_conf["map_resolution"],
                    input_shape=(512, 896)
                ),
            ),
        ),
        ins_decoder=dict(
            arch_name="mask2former",
            net_kwargs=dict(
                decoder_ids=[0, 1, 2, 3, 4, 5],
                in_channels=512,
                tgt_shape=(200, 100),
                num_feature_levels=1,
                mask_classification=True,
                num_classes=1,
                hidden_dim=512,
                num_queries=60,
                nheads=8,
                dim_feedforward=2048,
                dec_layers=6,
                pre_norm=False,
                mask_dim=512,
                enforce_input_project=False,
                use_local_map=use_local_map,
            ),
        ),
        output_head=dict(
            arch_name="bezier_output_head",
            net_kwargs=dict(
                in_channel=512,
                num_queries=[20, 25, 15],
                tgt_shape=map_conf['map_size'],
                num_degree=bezier_conf["num_degree"],
                max_pieces=bezier_conf["max_pieces"],
                bev_channels=512,
                ins_channel=64,
            )
        ),
        post_processor=dict(
            arch_name="bezier_post_processor",
            net_kwargs=dict(
                map_conf=map_conf,
                bezier_conf=bezier_conf,
                criterion_conf=dict(
                    bev_decoder=dict(
                        weight=[0.5, 0.8, 1.2, 1.8],
                        sem_mask_loss=dict(
                            ce_weight=1, dice_weight=1, use_point_render=True,
                            num_points=20000, oversample=3.0, importance=0.9)
                    ),
                    ins_decoder=dict(
                        weight=[0.4, 0.4, 0.4, 0.8, 1.2, 1.6],
                    ),
                    loss_weight=dict(
                        sem_loss=0.5,
                        obj_loss=2, ctr_loss=5, end_loss=2, msk_loss=5, curve_loss=10, recovery_loss=1)
                ),
                matcher_conf=dict(
                    cost_obj=2, cost_ctr=5, cost_end=2, cost_mask=5, cost_curve=10, cost_recovery=1,
                    ins_mask_loss_conf=dict(ce_weight=1, dice_weight=1,
                                            use_point_render=True, num_points=20000, oversample=3.0, importance=0.9),
                    point_loss_conf=dict(ce_weight=0, dice_weight=1, curve_width=5, tgt_shape=map_conf["map_size"])
                ),
                no_object_coe=0.5,
            )
        )
    )

    optimizer_setup = dict(
        base_lr=2e-4, wd=1e-4, backb_names=["backbone"], backb_lr=5e-5, extra_names=[], extra_lr=5e-5, freeze_names=[]
    )

    scheduler_setup = dict(milestones=[0.7, 0.9, 1.0], gamma=1 / 3)

    metric_setup = dict(
        class_names=CLASS_NAMES,
        map_resolution=map_conf["map_resolution"],
        iou_thicknesses=(1,),
        cd_thresholds=(0.2, 0.5, 1.0, 1.5, 5.0)
    )

    VAL_TXT = [
        # "assets/splits/argoverse2/geo_near_splits/av2_geosplit_val_interval_1.txt",
        "assets/splits/argoverse2/save_map_geo_val_PAO.txt",
    ]


class Exp(BaseExp):
    def __init__(self, batch_size_per_device=1, total_devices=8, max_epoch=60, **kwargs):
        super(Exp, self).__init__(batch_size_per_device, total_devices, max_epoch)

        self.exp_config = EXPConfig()
        self.seed = 0
        self.dump_interval = 1
        self.eval_interval = 1
        self.print_interval = 50
        self.data_loader_workers = 4
        self.num_keep_latest_ckpt = 1
        self.enable_tensorboard = True
        milestones = self.exp_config.scheduler_setup["milestones"]
        self.exp_config.scheduler_setup["milestones"] = [int(x * max_epoch) for x in milestones]
        lr_ratio_dict = {32: 2, 16: 1.5, 8: 1, 4: 1, 2: 0.5, 1: 0.5}
        assert total_devices in lr_ratio_dict, "Please set normal devices!"
        for k in ['base_lr', 'backb_lr', 'extra_lr']:
            self.exp_config.optimizer_setup[k] = self.exp_config.optimizer_setup[k] * lr_ratio_dict[total_devices]
        self.evaluation_save_dir = None

    def _configure_model(self):
        model = BeMapNet(self.exp_config.model_setup)
        return model

    def _configure_train_dataloader(self):
        from bemapnet.dataset.sampler import InfiniteSampler

        dataset_setup = self.exp_config.dataset_setup

        transform = Compose(
            [
                Normalize(**dataset_setup["img_norm_cfg"]),
                ToTensor(),
            ]
        )

        train_set = Argoverse2MapDataset(
            img_key_list=dataset_setup["img_key_list"],
            map_conf=self.exp_config.map_conf,
            ida_conf=self.exp_config.ida_conf,
            bezier_conf=self.exp_config.bezier_conf,
            transforms=transform,
            data_split="training",
            splits_txt="geo_near_splits/av2_geosplit_train_interval_1.txt",
        )

        sampler = None
        if is_distributed():
            sampler = InfiniteSampler(len(train_set), seed=self.seed if self.seed else 0)

        train_loader = torch.utils.data.DataLoader(
            train_set,
            batch_size=self.batch_size_per_device,
            pin_memory=True,
            num_workers=self.data_loader_workers,
            shuffle=sampler is None,
            drop_last=True,
            sampler=sampler,
        )
        self.train_dataset_size = len(train_set)
        return train_loader

    def _configure_val_dataloader(self):

        dataset_setup = self.exp_config.dataset_setup

        transform = Compose(
            [
                Normalize(**dataset_setup["img_norm_cfg"]),
                ToTensor(),
            ]
        )

        val_set = Argoverse2MapDataset(
            img_key_list=dataset_setup["img_key_list"],
            map_conf=self.exp_config.map_conf,
            ida_conf=self.exp_config.ida_conf,
            bezier_conf=self.exp_config.bezier_conf,
            transforms=transform,
            data_split="validation",
            # splits_txt="geo_near_splits/av2_geosplit_val_interval_1.txt",
            splits_txt='save_map_geo_val_PAO.txt',
        )

        sampler = None
        if is_distributed():
            sampler = DistributedSampler(val_set, shuffle=False)

        val_loader = torch.utils.data.DataLoader(
            val_set,
            batch_size=1,
            pin_memory=True,
            num_workers=self.data_loader_workers,
            shuffle=False,
            drop_last=False,
            sampler=sampler,
        )
        self.val_dataset_size = len(val_set)
        return val_loader

    def _configure_test_dataloader(self):
        pass

    def _configure_optimizer(self):
        optimizer_setup = self.exp_config.optimizer_setup
        optimizer = AdamW(get_param_groups(self.model, optimizer_setup))
        return optimizer

    def _configure_lr_scheduler(self):
        scheduler_setup = self.exp_config.scheduler_setup
        iters_per_epoch = len(self.train_dataloader)
        scheduler = MultiStepLR(
            optimizer=self.optimizer,
            gamma=scheduler_setup["gamma"],
            milestones=[int(v * iters_per_epoch) for v in scheduler_setup["milestones"]],
        )
        return scheduler

    def training_step(self, batch):
        batch["images"] = batch["images"].float().cuda()
        outputs = self.model(batch, "train")
        return self.model.module.post_processor(outputs["outputs"], batch["targets"])

    def test_step(self, batch):
        with torch.no_grad():
            batch["images"] = batch["images"].float().cuda()
            outputs = self.model(batch, "val")
            results, dt_masks, _ = self.model.module.post_processor(outputs["outputs"])
        self.save_results(batch["extra_infos"]["token"], results, dt_masks)

    # def save_results(self, tokens, results, dt_masks, epoch_num):
    def save_results(self, tokens, results, dt_masks):
        if self.evaluation_save_dir is None:
            self.evaluation_save_dir = os.path.join(self.output_dir, "evaluation", "results")
            if not os.path.exists(self.evaluation_save_dir):
                os.makedirs(self.evaluation_save_dir, exist_ok=True)
        for (token, dt_res, dt_mask) in zip(tokens, results, dt_masks):
            save_path = os.path.join(self.evaluation_save_dir, f"{token}.npz")
            np.savez_compressed(save_path, dt_res=dt_res, dt_mask=dt_mask)


if __name__ == "__main__":
    BeMapNetCli(Exp).run()
