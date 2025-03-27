import torch
import torch.nn as nn
from torch import Tensor, optim
from tqdm import tqdm
from tqdm.notebook import tqdm
from dataset import AnnDataSet
from torch.utils.data import DataLoader
import scanpy as sc
from torch.distributions import Normal
from torch.distributions import kl_divergence as kl
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import normalize
import ot
from torch.optim.lr_scheduler import StepLR
import torch.nn.functional as F
import gc


# from memory_profiler import profile


class scOTC(nn.Module):
    def __init__(self, input_dim=6998, latent_dim=200, hidden_dim=1000, 
                 noise_rate=0.1, kl_weight=5e-3, cycle_weight=0.01, num_heads=4, device=None):
        super(scOTC, self).__init__()
        if num_heads >= 1:
            self.attn = nn.MultiheadAttention(embed_dim=latent_dim, num_heads=num_heads, batch_first=True)
        self.device = device
        self.num_heads = num_heads
        self.cycle_weight = cycle_weight  
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.latent_dim = latent_dim
        self.noise_rate = noise_rate
        self.kl_weight = kl_weight
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu') if device is None else device

        # Encoder layers
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, latent_dim * 2)
        )

        # Decoder layers
        self.decoder = nn.Sequential(
            nn.Linear(latent_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, input_dim),
            nn.ReLU()
        )


    def encode(self, x):
        h = self.encoder(x)
        mu, logvar = torch.chunk(h, 2, dim=1)
        z = self.reparameterize(mu, logvar)
        if self.num_heads >= 1:
            z = z.unsqueeze(1) 
            attn_output, _ = self.attn(z, z, z)
            z = attn_output.squeeze(1) 
        return z, mu, logvar


    def decode(self, z):
        x_hat = self.decoder(z)
        return x_hat
    
    def reparameterize(self, mu, logvar):  
        std = torch.exp(0.5 * logvar) 
        eps = torch.randn_like(std)
        z = mu + eps * std 
        return z


    def forward(self, x):
        noise = torch.randn_like(x)
        x_noisy = x + noise * self.noise_rate  
        z, mu, logvar = self.encode(x_noisy)
        x_hat = self.decode(z)

        std = torch.exp(logvar / 2)
        loss_kl = kl(
            Normal(mu, std),
            Normal(0, 1)
        ).sum(dim=1)
        loss_rec = ((x - x_hat) ** 2).sum(dim=1)

        z_hat, _, _ = self.encode(x_hat)
        x_cycle = self.decode(z_hat)
        loss_cycle = ((x - x_cycle) ** 2).sum(dim=1)

        # total_loss = (0.4 * loss_rec + 0.3 * (loss_kl * self.kl_weight) + self.cycle_weight * loss_cycle).mean()
        return x_hat, loss_rec, loss_kl, loss_cycle
    

    def get_latent_adata(self, adata):
        device = self.device
        x = Tensor(adata.to_df().values).to(device)
        # except:
        #     x = Tensor(adata).to(device)
        latent_z = self.encode(x)[0].cpu().detach().numpy()
        latent_adata = sc.AnnData(X=latent_z, obs=adata.obs.copy())
        return latent_adata
    
    def get_latent(self, data):
        device = self.device
        # x = Tensor(data.to_df().values).to(device)
        latent_z = self.encode(data)[0].cpu().detach().numpy()
        latent_adata = sc.AnnData(X=latent_z, obs=data.obs.copy())
        return latent_adata

    def get_loss(self, x):
        x_hat, loss_rec, loss_kl, loss_cycle = self.forward(x)
        return x_hat,loss_rec, loss_kl, loss_cycle


    def train_scOTC(self, train_adata, epochs=100, batch_size=128, lr=5e-4, weight_decay=1e-5, wandb_run=None):
        device = self.device
        anndataset = AnnDataSet(train_adata)
        train_loader = DataLoader(anndataset, batch_size=batch_size, shuffle=True, drop_last=False)  # batch_size = 128
        scOTC_loss, loss_rec, loss_kl = 0, 0, 0
        optim_scOTC = optim.Adam(self.parameters(), lr=lr, weight_decay=weight_decay)
        scheduler = StepLR(optim_scOTC, step_size=1, gamma=0.99)
        pbar = tqdm(range(epochs))
        for epoch in pbar:
            pbar.set_description("Training Epoch {}".format(epoch))
            x_ = []
            x_hat_ = []
            loss_ = []
            loss_rec_ = []
            loss_kl_ = []
            loss_cycle_ = []
            for idx, (x,_) in enumerate(train_loader):   # x: torch.Size([128, 6998])
                x = x.to(device)
                x_hat, loss_rec, loss_kl, loss_cycle = self.get_loss(x)
                # scOTC_loss = (0.5 * loss_rec + 0.5 * (loss_kl * self.kl_weight)).mean()  # self.kl_weight = 0.0005
                scOTC_loss = (0.4 * loss_rec + 0.3 * (loss_kl * self.kl_weight) + self.cycle_weight * loss_cycle).mean()
                optim_scOTC.zero_grad()
                scOTC_loss.backward()
                torch.nn.utils.clip_grad_norm(self.parameters(), 10)
                optim_scOTC.step()
                loss_ += [scOTC_loss.item()]
                loss_rec_ += [loss_rec.mean().item()]
                loss_kl_ += [loss_kl.mean().item()]
                loss_cycle_ += [loss_cycle.mean().item()]
                # print(f'loss: {scOTC_loss.item()}')
            if wandb_run:
                wandb_run.log({"scOTC_loss": np.mean(loss_),
                               "recon_loss": np.mean(loss_rec_),
                               "kl_loss": np.mean(loss_kl_),
                               "cycle_loss": np.mean(loss_cycle_)})
            pbar.set_postfix(SCOPA_loss=np.mean(loss_), 
                             recon_loss=np.mean(loss_rec_),
                             kl_loss=np.mean(loss_kl_),
                             cycle_loss=np.mean(loss_cycle_))
            x_.append(x)
            x_hat_.append(x_hat)
            # scheduler.step()
        x_ = torch.cat(x_, dim=0)
        x_hat_ = torch.cat(x_hat_, dim=0)
        torch.cuda.empty_cache()
        gc.collect()
        return x_, x_hat_




    def predict_new(self, train_adata, cell_to_pred, key_dic, ratio=0.05, e=0, r=1):
        ctrl_to_pred = train_adata[((train_adata.obs[key_dic['cell_type_key']] == cell_to_pred) &
                                    (train_adata.obs[key_dic['condition_key']] == key_dic['ctrl_key']))]  
        
        
        ctrl_adata = train_adata[(train_adata.obs[key_dic['cell_type_key']] != cell_to_pred) &
                                 (train_adata.obs[key_dic['condition_key']] == key_dic['ctrl_key'])] 
        stim_adata = train_adata[(train_adata.obs[key_dic['condition_key']] == key_dic['stim_key'])]  

        ctrla = self.get_latent_adata(ctrl_adata) 
        stima = self.get_latent_adata(stim_adata)
        ctrl = ctrla.to_df().values
        stim = stima.to_df().values


        M = ot.dist(stim, ctrl, metric='euclidean') 
        G = ot.emd(torch.ones(stim.shape[0]) / stim.shape[0],
                   torch.ones(ctrl.shape[0]) / ctrl.shape[0], 
                   torch.tensor(M),  
                   numItermax=100000) 
        match_idx = torch.max(G, 0)[1].numpy() 
        stim_new = stim[match_idx] #
        delta_list = stim_new - ctrl # 

        mean_ctrl = torch.from_numpy(ctrl).to(self.device).mean(dim=0) 
        mean_stim = torch.from_numpy(stim).to(self.device).mean(dim=0) 
        mse_loss = nn.MSELoss()
        loss = mse_loss(mean_ctrl, mean_stim).item()

        test_za = self.get_latent_adata(ctrl_to_pred)  
        test_z = test_za.to_df().values
        
        cos_sim = cosine_similarity(np.array(test_z).reshape(-1, self.latent_dim),
                                    np.array(ctrl).reshape(-1, self.latent_dim))

        n_top = int(np.ceil(ctrl.shape[0] * ratio))  
        top_indices = np.argsort(cos_sim)[0][-n_top:] 
        normalized_weights = cos_sim[0][top_indices] / np.sum(cos_sim[0][top_indices]) 
        delta_pred = np.sum(normalized_weights[:, np.newaxis] * np.array(delta_list).reshape(-1, self.latent_dim)[top_indices], axis=0)
        pred_z = test_z + r*delta_pred 
        if e:
            pred_z = pred_z + e*ratio*loss
        pred_x = self.decode(Tensor(pred_z).to(self.device)).cpu().detach().numpy()
        pred_adata = sc.AnnData(X=pred_x, obs=ctrl_to_pred.obs.copy(), var=ctrl_to_pred.var.copy())
        pred_adata.obs[key_dic['condition_key']] = key_dic['pred_key']
        return pred_adata,ctrla,stima,test_za


