from torch.utils.data import Dataset, DataLoader
import numpy as np
import scanpy as sc





class AnnDataSet(Dataset):
    def __init__(self, adata):
        '''
        Build dataset of adata
        :param adata: adata of training or testing set
        '''
        self.data = adata.to_df().values
        try:
            self.cell_type = adata.obs['cell_type']  # PBMC study
        except KeyError:
            try:
                self.cell_type = adata.obs['cell_label']  # Hpoly 
            except KeyError:
                self.cell_type = adata.obs['louvain']  # species 
        # self.D_label = adata.obs['condition'] 
        # celltype to index
        unique_labels = self.cell_type.unique()  
        self.label_mapping = {label: idx for idx, label in enumerate(unique_labels)}
        # map celltype to index
        self.cell_type = self.cell_type.map(self.label_mapping).values

        # # map condition to index
        # D_label = self.D_label
        # unique_D_labels = D_label.unique()
        # self.D_label_mapping = {label: idx for idx, label in enumerate(unique_D_labels)}
        # self.D_label = self.D_label.map(self.D_label_mapping).values

    def __getitem__(self, index):
        x = self.data[index, :]
        y = self.cell_type[index]
        # D_label = self.D_label[index]
        return x, y

    def __len__(self):
        return self.data.shape[0]

