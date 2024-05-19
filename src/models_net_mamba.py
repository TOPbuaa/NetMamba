import torch
import torch.nn as nn
from timm.models.layers import DropPath
from models_mamba import create_block, RMSNorm, rms_norm_fn, PACKET_NUM, StrideEmbed
from timm.models.layers import trunc_normal_


class NetMamba(nn.Module):
    def __init__(self, img_size=40, stride_size=4, in_chans=1,
                 embed_dim=192, depth=4, 
                 decoder_embed_dim=128, decoder_depth=2,
                 num_classes=1000,
                 norm_pix_loss=False,
                 drop_path_rate=0.1,
                 bimamba_type="none",
                 is_pretrain=False,
                 device=None, dtype=None,
                 **kwargs):
        super().__init__()

        factory_kwargs = {"device": device, "dtype": dtype}
        # add factory_kwargs into kwargs
        kwargs.update(factory_kwargs) 
        self.num_classes = num_classes
        self.d_model = self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models
        self.is_pretrain = is_pretrain

        # --------------------------------------------------------------------------
        # NetMamba encoder specifics
        self.patch_embed = StrideEmbed(img_size, img_size, stride_size, in_chans, embed_dim)
        self.num_patches = self.patch_embed.num_patches
        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        num_cls_token = 1
        self.pos_embed = nn.Parameter(torch.zeros(1, self.num_patches + num_cls_token, embed_dim))
        # Mamba blocks
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        inter_dpr = [0.0] + dpr
        self.drop_path = DropPath(drop_path_rate) if drop_path_rate > 0. else nn.Identity()
        self.blocks = nn.ModuleList([
            create_block(
                embed_dim,
                ssm_cfg=None,
                norm_epsilon=1e-5,
                rms_norm=True,
                residual_in_fp32=True,
                fused_add_norm=True,
                layer_idx=i,
                if_bimamba=False,
                bimamba_type=bimamba_type,
                drop_path=inter_dpr[i],
                if_devide_out=True,
                init_layer_scale=None,
            )  for i in range(depth)])
        self.norm_f = RMSNorm(embed_dim, eps=1e-5)
        # --------------------------------------------------------------------------

        if is_pretrain:
            # --------------------------------------------------------------------------
            # NetMamba decoder specifics
            self.decoder_embed = nn.Linear(embed_dim, decoder_embed_dim, bias=True)
            self.mask_token = nn.Parameter(torch.zeros(1, 1, decoder_embed_dim))
            self.decoder_pos_embed = nn.Parameter(torch.zeros(1, self.num_patches + num_cls_token, decoder_embed_dim))
            decoder_dpr = [x.item() for x in torch.linspace(0, drop_path_rate, decoder_depth)]  # stochastic depth decay rule
            decoder_inter_dpr = [0.0] + decoder_dpr
            self.decoder_blocks = nn.ModuleList([
                create_block(
                    decoder_embed_dim,
                    ssm_cfg=None,
                    norm_epsilon=1e-5,
                    rms_norm=True,
                    residual_in_fp32=True,
                    fused_add_norm=True,
                    layer_idx=i,
                    if_bimamba=False,
                    bimamba_type=bimamba_type,
                    drop_path=decoder_inter_dpr[i],
                    if_devide_out=True,
                    init_layer_scale=None,
                )
                for i in range(decoder_depth)])
            self.decoder_norm_f = RMSNorm(decoder_embed_dim, eps=1e-5)
            self.decoder_pred = nn.Linear(decoder_embed_dim, stride_size * in_chans, bias=True)  # decoder to stride
            # --------------------------------------------------------------------------
        else:
            # --------------------------------------------------------------------------
            # NetMamba classifier specifics
            self.head = nn.Linear(self.num_features, num_classes) if num_classes > 0 else nn.Identity()
            # --------------------------------------------------------------------------

        self.norm_pix_loss = norm_pix_loss
        self.initialize_weights()

    def initialize_weights(self):
        # initialization
        trunc_normal_(self.pos_embed, std=.02)
        trunc_normal_(self.decoder_pos_embed, std=.02)

        # initialize patch_embed like nn.Linear (instead of nn.Conv2d)
        w = self.patch_embed.proj.weight.data
        torch.nn.init.xavier_uniform_(w.view([w.shape[0], -1]))

        # timm's trunc_normal_(std=.02) is effectively normal_(std=0.02) as cutoff is too big (2.)
        torch.nn.init.normal_(self.cls_token, std=.02)
        torch.nn.init.normal_(self.mask_token, std=.02)

        # initialize nn.Linear and nn.LayerNorm
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            # we use xavier_uniform following official JAX ViT:
            torch.nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Embedding):
            nn.init.normal_(m.weight, std=0.02)
    
    def stride_patchify(self, imgs, stride_size=4):
        """
        imgs: (N, 1, H, W)
        x: (N, L, patch_size**2 *1)
        """
        B, C, H, W = imgs.shape
        assert C == 1, "Input images should be grayscale"
        x = imgs.reshape(B, H*W // stride_size, stride_size)
        return x

    def random_masking(self, x, mask_ratio):
        """
        Perform per-sample random masking by per-sample shuffling.
        Per-sample shuffling is done by argsort random noise.
        x: [B N D], sequence
        """
        B, N, D = x.shape  # batch, length, dim
        len_keep = int(N * (1 - mask_ratio))

        noise = torch.rand(B, N, device=x.device)  # noise in [0, 1]

        # sort noise for each sample
        ids_shuffle = torch.argsort(noise, dim=1)  # ascend: small is keep, large is remove
        ids_restore = torch.argsort(ids_shuffle, dim=1) # ids_restore[i] = i-th noise element's rank

        # keep the first subset
        ids_keep = ids_shuffle[:, :len_keep]
        x_masked = torch.gather(x, dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, D)) # x_masked are acctually non-masked elements

        # generate the binary mask: 0 is keep, 1 is remove
        mask = torch.ones([B, N], device=x.device)
        mask[:, :len_keep] = 0
        # unshuffle to get the binary mask
        mask = torch.gather(mask, dim=1, index=ids_restore)

        return x_masked, mask, ids_restore

    def forward_encoder(self, x, mask_ratio, if_mask=True,):
        """
        x: [B, 1, H, W]
        """
        # embed patches
        B, C, H, W = x.shape
        x = self.patch_embed(x.reshape(B, C, -1))

        # add pos embed w/o cls token
        x = x + self.pos_embed[:, :-1, :]

        # masking: length -> length * mask_ratio
        if if_mask:
            x, mask, ids_restore = self.random_masking(x, mask_ratio)

        # append cls token
        cls_token = self.cls_token + self.pos_embed[:, -1, :]
        cls_tokens = cls_token.expand(x.shape[0], -1, -1)
        x = torch.cat((x, cls_tokens), dim=1)

        # apply Mamba blocks
        residual = None
        hidden_states = x
        for blk in self.blocks:
            hidden_states, residual = blk(hidden_states, residual)
        fused_add_norm_fn = rms_norm_fn
        x = fused_add_norm_fn(
            self.drop_path(hidden_states),
            self.norm_f.weight,
            self.norm_f.bias,
            eps=self.norm_f.eps,
            residual=residual,
            prenorm=False,
            residual_in_fp32=True,
        )
        if if_mask:
            return x, mask, ids_restore
        else:
            return x

    def forward_decoder(self, x, ids_restore):
        # embed tokens
        x = self.decoder_embed(x)

        # append mask tokens to sequence
        mask_tokens = self.mask_token.repeat(x.shape[0], ids_restore.shape[1] + 1 - x.shape[1], 1)
        visible_tokens = x[:, :-1, :]
        x_ = torch.cat([visible_tokens, mask_tokens], dim=1)  # no cls token
        x_ = torch.gather(x_, dim=1, index=ids_restore.unsqueeze(-1).repeat(1, 1, x.shape[2]))  # unshuffle
        x = torch.cat([x_, x[:, -1:, :]], dim=1)  # append cls token

        # add pos embed
        x = x + self.decoder_pos_embed

        # apply Mamba blocks
        residual = None
        hidden_states = x
        for blk in self.decoder_blocks:
            hidden_states, residual = blk(hidden_states, residual)
        fused_add_norm_fn = rms_norm_fn
        x = fused_add_norm_fn(
            self.drop_path(hidden_states),
            self.decoder_norm_f.weight,
            self.decoder_norm_f.bias,
            eps=self.decoder_norm_f.eps,
            residual=residual,
            prenorm=False,
            residual_in_fp32=True,
        )

        # predictor projection
        x = self.decoder_pred(x)

        # remove cls token
        x = x[:, :-1, :]
        return x

    def forward_rec_loss(self, imgs, pred, mask):
        """
        imgs: [N, 1, H, W]
        pred: [N, L, p*p*1]
        mask: [N, L], 0 is keep, 1 is remove,
        """
        target = self.stride_patchify(imgs)
        if self.norm_pix_loss:
            mean = target.mean(dim=-1, keepdim=True)
            var = target.var(dim=-1, keepdim=True)
            target = (target - mean) / (var + 1.e-6) ** .5

        loss = (pred - target) ** 2
        loss = loss.mean(dim=-1)  # [N, L], mean loss per patch

        loss = (loss * mask).sum() / mask.sum()  # mean loss on removed patches
        return loss

    def forward(self, imgs, mask_ratio=0.9,):
        # imgs: [B, 1, H, W]
        B, C, H, W = imgs.shape
        assert C == 1, "Input images should be grayscale"
        if self.is_pretrain:
            latent, mask, ids_restore = self.forward_encoder(imgs, 
                    mask_ratio=mask_ratio,)
            pred = self.forward_decoder(latent, ids_restore)
            loss = self.forward_rec_loss(imgs, pred, mask)
            return loss, pred, mask
        else:
            x = self.forward_encoder(imgs, mask_ratio=mask_ratio, if_mask=False)
            return self.head(x[:, -1, :])
        

def net_mamba_pretrain(**kwargs):
    model = NetMamba(
        is_pretrain=True, img_size=40, stride_size=4, embed_dim=256, depth=4,
        decoder_embed_dim=128, decoder_depth=2, **kwargs)
    return model

def net_mamba_classifier(**kwargs):
    model = NetMamba(
        is_pretrain=False, img_size=40, stride_size=4, embed_dim=256, depth=4,
        **kwargs)
    return model