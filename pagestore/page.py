import os

import numpy as np
import pickle

from .data import Data


def num2path(num):
    ss=str(num)
    if len(ss)%2==1:
        ss='0'+ss
    lst= [a+b for a,b in zip(ss[::2],ss[1::2])]
    return os.path.join(*lst)

class Page:
    @classmethod
    def from_data(cls,pageid,data):
        begin=data.idx[0]
        end=data.idx[-1]
        count=len(data)
        size=data.get_size()
        idx_type=data.idx.dtype.str
        name=data.name
        return cls(pageid,name,begin,end,count,size,idx_type)

    def __init__(self,pageid,name,begin,end,count,size,idx_type):
        self.pageid=pageid
        self.name=name
        self.begin=begin
        self.end=end
        self.count=count
        self.size=size
        self.idx_type=idx_type

    def to_list(self):
        return self.pageid,self.name,self.begin,self.end,self.count,self.size,self.idx_type

    def __repr__(self):
        ss=', '.join(map(str,self.to_list()))
        return f"Page({ss})"

    def get_prefix(self,pagedir):
        return os.path.join(pagedir,num2path(self.pageid))

    def create_file(self,pagedir,extension):
        filename=self.get_prefix(pagedir)+extension
        head,tail= os.path.split(filename)
        os.makedirs(head,exist_ok=True)
        return filename

    def read_idx(self,pagedir):
        filename=self.get_prefix(pagedir)+'.idx'
        return np.fromfile(filename, dtype=self.idx_type, count=self.count)

    def write_idx(self,idx,pagedir):
        filename=self.create_file(pagedir,'.idx')
        idx.tofile(filename)

    def read_rec(self,pagedir):
        filename=self.get_prefix(pagedir)+'.rec'
        return pickle.load(open(filename,'rb'))

    def write_rec(self,rec,pagedir):
        filename=self.get_prefix(pagedir)+'.rec'
        pickle.dump(rec,open(filename,'wb'))

    def read_meta(self,pagedir):
        filename=self.get_prefix(pagedir)+'.page'
        return pickle.load(open(filename,'rb'))

    def write_meta(self,pagedir):
        filename=self.get_prefix(pagedir)+'.page'
        pickle.dump(self,open(filename,'wb'))

    def read(self,pagedir):
        idx=self.read_idx(pagedir)
        rec=self.read_rec(pagedir)
        return Data(idx,rec,self.name)

    def check(self,pagedir):
        page=self.read_meta(pagedir)
        self.compare(page)
        data=self.read(pagedir)
        page=Page.from_data(self.pageid,data)
        self.compare(page)

    def write(self,data,pagedir):
        self.write_idx(data.idx,pagedir)
        self.write_rec(data.rec,pagedir)
        self.write_meta(pagedir)

    def delete(self,pagedir):
        os.unlink(self.get_prefix(pagedir)+'.idx')
        os.unlink(self.get_prefix(pagedir)+'.rec')
        os.unlink(self.get_prefix(pagedir)+'.page')

    def check_with_data(self,data):
        assert self.begin==data.idx[0]
        assert self.end==data.idx[-1]
        assert self.count==len(data)
        assert self.size==data.get_size()
        assert self.idx_type==data.idx.dtype.str
        assert self.name==data.name

    def compare(self,page):
        for k in page.__dict__:
            assert getattr(self,k)==getattr(page,k)
        return True






