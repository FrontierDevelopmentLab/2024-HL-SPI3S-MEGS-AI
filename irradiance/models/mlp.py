from irradiance.models.kan_success import KANDEMSpectrum, BaseDEMModel
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import HuberLoss
from pytorch_optimizer import create_optimizer
from typing import *


class MLPDEMSpectrum(BaseDEMModel):
    def __init__(
        self,
        eve_norm,
        uv_norm,
        wavelengths,
        t_query_points,
        kanfov,
        layers_hidden_dem: List[int],
        layers_hidden_sp: List[int],
        base_activation = F.silu,
        use_layernorm = False,
        base_temp_exponent=0,
        intensity_factor=1e25,
        lr=1e-4,
        loss_func = HuberLoss(),
        stride=None
    ) -> None:
        super().__init__(eve_norm=eve_norm, 
                         uv_norm=uv_norm, 
                         wavelengths=wavelengths, 
                         kanfov=kanfov, 
                         model=None, 
                         t_query_points=t_query_points, 
                         loss_func=loss_func, 
                         lr=lr,
                         base_temp_exponent=base_temp_exponent,
                         intensity_factor = intensity_factor,
                         stride=stride)
        self.save_hyperparameters()
        self.use_layer_norm = use_layernorm
        self.base_activation = base_activation

        # specify the Linear model
        self.dem_layers = nn.ModuleList([
                nn.Linear(
                    in_dim, out_dim,
                ) for in_dim, out_dim  in zip(layers_hidden_dem[:-1], layers_hidden_dem[1:])
            ])            
        
        self.spectrum_layers = nn.ModuleList([
                nn.Linear(
                    in_dim, out_dim,
                ) for in_dim, out_dim in zip(layers_hidden_sp[:-1], layers_hidden_sp[1:])
            ])
        
        self.eve_calibration = nn.Parameter(torch.Tensor([1.0]))
    
    
    def forward_unnormalize(self, x):
        intensity, dem, intensity_target = self.intensity_calculation(x) # dem: batch, channel, t_query_points
        dem = dem[:, :, :, 0] # batch, pixels, t_query_points
        spectrum = self.forward_spectrum(dem)
        spectrum = self.eve_calibration*torch.mean(spectrum, dim=1)
        return self.unnormalize(spectrum, self.eve_norm)
    
    def forward(self, x):
        for layer in self.dem_layers[:-1]:
            if self.use_layer_norm:
                x = nn.LayerNorm(layer(x))
            x = self.base_activation(layer(x))
        if self.use_layer_norm:
            x = nn.LayerNorm(x)
        x = self.dem_layers[-1](x)
        return F.relu(x)

    def forward_spectrum(self, x):
        for layer in self.spectrum_layers[:-1]:
            if self.use_layer_norm:
                x = nn.LayerNorm(layer(x))
            x = self.base_activation(layer(x))
        if self.use_layer_norm:
            x = nn.LayerNorm(x)
        x = self.spectrum_layers[-1](x)
        return x

    
    def training_step(self, batch, batch_nb):
        x, y = batch

        intensity, dem, intensity_target = self.intensity_calculation(x) # dem: batch, channel, t_query_points
        dem = dem[:, :, :, 0] # batch, pixels, t_query_points
        spectrum = self.forward_spectrum(dem)
        spectrum = self.eve_calibration*torch.mean(spectrum, dim=1)
        # Compare with target
        loss_dem = self.loss_func(intensity, intensity_target)
        loss_sp = self.loss_func(spectrum, y)

        # Add log loss
        loss_log_sp = self.loss_func(torch.log(self.unnormalize(spectrum, self.eve_norm)), torch.log(self.unnormalize(y, self.eve_norm)))

        rae_dem = torch.abs((intensity_target - intensity) / (torch.abs(intensity_target))) * 100
        rae_sp = torch.abs((y - spectrum) / (torch.abs(y))) * 100
        self.log("train_loss_dem", loss_dem, on_epoch=True, prog_bar=True, logger=True)
        self.log("train_RAE_dem", torch.mean(rae_dem[torch.isfinite(rae_dem)]), on_epoch=True, prog_bar=True, logger=True)
        self.log("train_loss_sp", loss_sp, on_epoch=True, prog_bar=True, logger=True)
        self.log("train_RAE_sp", torch.mean(rae_sp[torch.isfinite(rae_sp)]), on_epoch=True, prog_bar=True, logger=True)
        self.log("train_log_loss_sp", loss_log_sp, on_epoch=True, prog_bar=True, logger=True)
        self.log("train_loss", loss_dem + loss_sp + loss_log_sp, on_epoch=True, prog_bar=True, logger=True)

        return loss_dem + loss_sp + loss_log_sp

    
    def validation_step(self, batch, batch_nb):
        x, y = batch

        intensity, dem, intensity_target = self.intensity_calculation(x)
        dem = dem[:, :, :, 0] # batch, pixels, t_query_points
        spectrum = self.forward_spectrum(dem)
        spectrum = self.eve_calibration*torch.mean(spectrum, dim=1)
        # Compare with target
        loss_dem = self.loss_func(intensity, intensity_target)
        loss_sp = self.loss_func(spectrum, y)

        # Add log loss
        loss_log_sp = self.loss_func(torch.log(self.unnormalize(spectrum, self.eve_norm)), torch.log(self.unnormalize(y, self.eve_norm)))

        rae_dem = torch.abs((intensity_target - intensity) / (torch.abs(intensity_target))) * 100
        rae_sp = torch.abs((y - spectrum) / (torch.abs(y))) * 100
        mae_dem = torch.abs(intensity_target - intensity).mean()
        mae_sp = torch.abs(y - spectrum).mean()
        self.log("valid_loss_dem", loss_dem, on_epoch=True, prog_bar=True, logger=True)
        self.log("valid_RAE_dem", torch.mean(rae_dem[torch.isfinite(rae_dem)]), on_epoch=True, prog_bar=True, logger=True)
        self.log("valid_loss_sp", loss_sp, on_epoch=True, prog_bar=True, logger=True)
        self.log("valid_RAE_sp", torch.mean(rae_sp[torch.isfinite(rae_sp)]), on_epoch=True, prog_bar=True, logger=True)
        self.log("valid_MAE_dem", mae_dem, on_epoch=True, prog_bar=True, logger=True)
        self.log("valid_MAE_sp", mae_sp, on_epoch=True, prog_bar=True, logger=True)
        self.log("valid_log_loss_sp", loss_log_sp, on_epoch=True, prog_bar=True, logger=True)
        self.log("valid_loss", loss_dem + loss_sp + loss_log_sp, on_epoch=True, prog_bar=True, logger=True)

        return loss_dem + loss_sp + loss_log_sp

    def configure_optimizers(self):
        optimizer = create_optimizer(
            self,
            'adamp',
            lr=1e-4,
            use_gc=True,
            use_lookahead=True,
        )
        return optimizer