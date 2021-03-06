"""
Train a HUMUS-Net model on the fastMRI dataset. 

Code based on https://github.com/facebookresearch/fastMRI/fastmri_examples/varnet/train_varnet_demo.py
"""
import os, sys
import pathlib
from argparse import ArgumentParser
import json
#sys.path.insert(0, os.path.dirname(pathlib.Path(__file__).parent.absolute())   )

import pytorch_lightning as pl
from fastmri.data.mri_data import fetch_dir
from fastmri.data.subsample import create_mask_for_mask_type

from data.data_transforms import HUMUSNetDataTransform
from pl_modules.fastmri_data_module import FastMriDataModule

# Imports for logging and other utility
from pytorch_lightning.plugins import DDPPlugin
import yaml
import torch.distributed
from  pl_modules.humus_module import HUMUSNetModule
import yaml

def load_args_from_config(args):
    config_file = args.config_file
    if config_file.exists():
        with config_file.open('r') as f:
            d = yaml.safe_load(f)
            for k,v in d.items():
                setattr(args, k, v)
    else:
        print('Config file does not exist.')
    return args


def cli_main(args):
    if args.verbose:
        print(args.__dict__)
        
    pl.seed_everything(args.seed)
    # ------------
    # model
    # ------------
    if args.challenge == 'multicoil':
        model = HUMUSNetModule(
            num_cascades=args.num_cascades,
            sens_pools=args.sens_pools,
            sens_chans=args.sens_chans,
            img_size=args.uniform_train_resolution,
            patch_size=args.patch_size,
            window_size=args.window_size,
            embed_dim=args.embed_dim, 
            depths=args.depths,
            num_heads=args.num_heads,
            mlp_ratio=args.mlp_ratio, 
            bottleneck_depth=args.bottleneck_depth,
            bottleneck_heads=args.bottleneck_heads,
            resi_connection=args.resi_connection,
            conv_downsample_first=args.conv_downsample_first,
            num_adj_slices=args.num_adj_slices,
            mask_center=(not args.no_center_masking),
            use_checkpoint=args.use_checkpointing,
            lr=args.lr,
            lr_step_size=args.lr_step_size,
            lr_gamma=args.lr_gamma,
            weight_decay=args.weight_decay,
        )
    else:
        raise ValueError('Singlecoil acquisition not supported yet for HUMUS-Net.')

    
    # ------------
    # data
    # ------------
    # this creates a k-space mask for transforming input data
    mask = create_mask_for_mask_type(
        args.mask_type, args.center_fractions, args.accelerations
    )
    
    # use random masks for train transform, fixed masks for val transform
    train_transform = HUMUSNetDataTransform(uniform_train_resolution=args.uniform_train_resolution, mask_func=mask, use_seed=False)
    val_transform = HUMUSNetDataTransform(uniform_train_resolution=args.uniform_train_resolution, mask_func=mask)
    test_transform = HUMUSNetDataTransform(uniform_train_resolution=args.uniform_train_resolution)
    
    # ptl data module - this handles data loaders
    data_module = FastMriDataModule(
        data_path=args.data_path,
        challenge=args.challenge,
        train_transform=train_transform,
        val_transform=val_transform,
        test_transform=test_transform,
        test_split=args.test_split,
        test_path=args.test_path,
        sample_rate=args.sample_rate,
        volume_sample_rate=args.volume_sample_rate,
        use_dataset_cache_file=args.use_dataset_cache_file,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        distributed_sampler=(args.accelerator in ("ddp", "ddp_cpu")),
        combine_train_val=args.combine_train_val,
        train_scanners=args.train_scanners,
        val_scanners=args.val_scanners,
        combined_scanner_val=args.combined_scanner_val,
        num_adj_slices=args.num_adj_slices,
    )

    # ------------
    # trainer
    # ------------
    trainer = pl.Trainer.from_argparse_args(args, 
                                            plugins=DDPPlugin(find_unused_parameters=False),
                                            checkpoint_callback=True,
                                            callbacks=args.checkpoint_callback)
    
    # Save all hyperparameters to .yaml file in the current log dir
    if torch.distributed.is_available():
        if torch.distributed.is_initialized():
            if torch.distributed.get_rank() == 0:
                save_all_hparams(trainer, args)
    else: 
         save_all_hparams(trainer, args)
            
    # ------------
    # run
    # ------------
    trainer.fit(model, datamodule=data_module)

def save_all_hparams(trainer, args):
    if not os.path.exists(trainer.logger.log_dir):
        os.makedirs(trainer.logger.log_dir)
    save_dict = args.__dict__
    save_dict.pop('checkpoint_callback')
    with open(trainer.logger.log_dir + '/hparams.yaml', 'w') as f:
        yaml.dump(save_dict, f)
    
def build_args():
    parser = ArgumentParser()

    # basic args
    num_gpus = 2 if backend == "ddp" else 1
    batch_size = 1

    # client arguments
    parser.add_argument(
        '--config_file', 
        default=None,   
        type=pathlib.Path,          
        help='If given, experiment configuration will be loaded from this yaml file.',
    )
    parser.add_argument(
        '--verbose', 
        default=False,   
        action='store_true',          
        help='If set, print all command line arguments at startup.',
    )

    # data transform params
    parser.add_argument(
        "--mask_type",
        choices=("random", "equispaced"),
        default="equispaced",
        type=str,
        help="Type of k-space mask",
    )
    parser.add_argument(
        "--center_fractions",
        nargs="+",
        default=[0.04],
        type=float,
        help="Number of center lines to use in mask",
    )
    parser.add_argument(
        "--accelerations",
        nargs="+",
        default=[8],
        type=int,
        help="Acceleration rates to use for masks",
    )

    # data config
    parser = FastMriDataModule.add_data_specific_args(parser)
    parser.set_defaults(
        mask_type="random",  # random masks for knee data
        batch_size=batch_size,  # number of samples per batch
        test_path=None,  # path for test split, overwrites data_path
    )

    # module config
    parser = HUMUSNetModule.add_model_specific_args(parser)
    parser.set_defaults(
        num_cascades=6,  # number of unrolled iterations
        pools=4,  # number of pooling layers for U-Net
        chans=18,  # number of top-level channels for U-Net
        sens_pools=4,  # number of pooling layers for sense est. U-Net
        sens_chans=8,  # number of top-level channels for sense est. U-Net
        lr=0.0003,  # Adam learning rate
        lr_step_size=40,  # epoch at which to decrease learning rate
        lr_gamma=0.1,  # extent to which to decrease learning rate
        weight_decay=0.0,  # weight regularization strength
    )
    
    #         self.sm_training_data_dir = os.environ.get("SM_CHANNEL_TRAINING")
#         self.sm_output_data_dir = os.environ.get("SM_OUTPUT_DATA_DIR")
#         self.sm_checkpoint_dir = os.environ.get("SM_CHECKPOINT_DIR")
#         self.sm_model_dir = os.environ.get("SM_MODEL_DIR")
#         self.sm_hosts = os.environ.get("SM_HOSTS", "[\"localhost\"]")
#         self.num_nodes = len(json.loads(self.sm_hosts))

    # trainer config
    parser = pl.Trainer.add_argparse_args(parser)
    parser.set_defaults(
        gpus=-1,  # number of gpus to use
        nodes=len(json.loads(os.environ.get("SM_HOSTS", "[\"localhost\"]")))
        replace_sampler_ddp=False,  # this is necessary for volume dispatch during val
        accelerator='ddp',  # what distributed version to use
        seed=42,  # random seed
        deterministic=True,  # makes things slower, but deterministic
    )

    args = parser.parse_args()
    
    # Load args if config file is given
    args = load_args_from_config('humus_examples/experiments/fastmri/humus_default.yaml')
    args.data_path = pathlib.Path(args.data_path)
#     if args.config_file is not None:
#         args = load_args_from_config(args)
        

    args.checkpoint_callback = pl.callbacks.ModelCheckpoint(
        save_top_k=1,
        verbose=True,
        monitor="val_metrics/ssim",
        mode="max",
        filename='epoch{epoch}-ssim{val_metrics/ssim:.4f}',
        auto_insert_metric_name=False,
        save_last=True
    )

    return args


def run_cli():
    args = build_args()

    # ---------------------
    # RUN TRAINING
    # ---------------------
    cli_main(args)


if __name__ == "__main__":
    run_cli()

# import json
# import logging
# import os
# import torch

# from pytorch_lightning.loggers import TensorBoardLogger
# from pytorch_lightning.utilities.cli import LightningCLI, LightningArgumentParser

# from app.model import ResNet18


# logger = logging.getLogger('pytorch_lightning')


# class CLI(LightningCLI):
#     def __init__(self, *args, **kwargs):
#         self.sm_training_data_dir = os.environ.get("SM_CHANNEL_TRAINING")
#         self.sm_output_data_dir = os.environ.get("SM_OUTPUT_DATA_DIR")
#         self.sm_checkpoint_dir = os.environ.get("SM_CHECKPOINT_DIR")
#         self.sm_model_dir = os.environ.get("SM_MODEL_DIR")
#         self.sm_hosts = os.environ.get("SM_HOSTS", "[\"localhost\"]")
#         self.num_nodes = len(json.loads(self.sm_hosts))
#         super().__init__(*args, **kwargs)

#     @property
#     def last_checkpoint_path(self):
#         if self.sm_checkpoint_dir:
#             return os.path.join(self.sm_checkpoint_dir, 'last.ckpt')

#     @property
#     def model_checkpoint_config(self):
#         for callback_config in self.config["trainer"]["callbacks"]:
#             class_path = callback_config.get("class_path")
#             if "ModelCheckpoint" in class_path:
#                 return callback_config

#     def before_instantiate_classes(self) -> None:
#         if self.sm_training_data_dir:
#             # Update config (instead of setting parser defaults) because
#             # data module class is set dynamically as command line option.
#             self.config["data"]["init_args"]["data_dir"] = self.sm_training_data_dir

#         if self.sm_checkpoint_dir:
#             logger.info(f'Update checkpoint callback to write to {self.sm_checkpoint_dir}')
#             self.model_checkpoint_config['init_args']['dirpath'] = self.sm_checkpoint_dir

#     def add_arguments_to_parser(self, parser: LightningArgumentParser):
#         # Bind num_classes property of the data module to model's num_classes parameter.
#         parser.link_arguments("data.num_classes", "model.num_classes", apply_on="instantiate")

#         # Make TensorBoardLogger configurable under the "logger" namespace and
#         # expose flush_secs keyword argument as additional command line option.
#         parser.add_class_arguments(TensorBoardLogger, "logger")
#         parser.add_argument("--logger.flush_secs", default=60, type=int)

#         if self.sm_output_data_dir:
#             parser.set_defaults({
#                 "trainer.weights_save_path": os.path.join(self.sm_output_data_dir, "checkpoints"),
#                 "logger.save_dir": os.path.join(self.sm_output_data_dir, "tensorboard")
#             })

#     def instantiate_trainer(self, **kwargs):
#         # Instantiate trainer with configured logger and number of nodes as arguments.
#         return super().instantiate_trainer(logger=self.config_init["logger"], num_nodes=self.num_nodes, **kwargs)


# def main():
#     trainer_defaults = {
#         # Trainer default configuration is defined in file app/trainer.yaml.
#         "default_config_files": [os.path.join("app", "trainer.yaml")]
#     }

#     # Instantiate trainer, model and data module.
#     cli = CLI(model_class=ResNet18, parser_kwargs=trainer_defaults, save_config_overwrite=True, run=False)

#     if cli.last_checkpoint_path and os.path.exists(cli.last_checkpoint_path):
#         logger.info(f'Resume training from checkpoint {cli.last_checkpoint_path}')
#         cli.trainer.fit(cli.model, cli.datamodule, ckpt_path=cli.last_checkpoint_path)
#     else:
#         logger.info('Start training from scratch')
#         cli.trainer.fit(cli.model, cli.datamodule)

#     if cli.trainer.is_global_zero and cli.sm_model_dir:
#         # Load best checkpoint.
#         best_checkpoint_path = cli.trainer.checkpoint_callback.best_model_path
#         best_checkpoint = ResNet18.load_from_checkpoint(best_checkpoint_path)

#         # Write best model to SageMaker model directory.
#         best_model_path = os.path.join(cli.sm_model_dir, "model.pt")
#         torch.save(best_checkpoint.model.state_dict(), best_model_path)

#         os.remove(best_checkpoint_path)


# if __name__ == "__main__":
#     main()
