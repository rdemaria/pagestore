import numpy as np

class Data:
    """
    Constraints:
        len(idx)==len(rec)
        idx is sorted (assumed but not checked)
    """
    @staticmethod
    def is_data(data):
        return  hasattr(data,'idx') and hasattr(data,'rec') and hasattr(data,'name')

    def __init__(self,idx,rec,name=''):
        self.idx=np.array(idx)
        self.rec=np.array(rec)
        if len(self.rec)!=len(self.idx):
            raise ValueError(f"Length mismatch idx/rec {len(self.idx)}!={len(self.rec)}")
        self.name=name

    def sort(self):
        args=np.argsort(self.idx)
        self.idx=self.idx[args]
        self.rec=self.rec[args]

    def cut_idx(self,icut):
        left=Data(self.idx[:icut],self.rec[:icut],self.name)
        right=Data(self.idx[icut:],self.rec[icut:],self.name)
        return left,right

    def cut_lt(self,threshold):
        ic=np.where(self.idx<threshold)[0][-1]
        return self.cut_idx(ic+1)

    def cut_nbytes(self,nbytes):
        guess=int(nbytes/self.get_size()*len(self))
        while self.rec[:guess].nbytes>nbytes and guess>0:
            guess=int(guess*0.9)
        if guess==0:
            guess=1
        return self.cut_idx(guess)

    def __len__(self):
        return len(self.idx)

    def begin(self):
        return self.idx[0]

    def end(self):
        return self.idx[-1]

    def filter(self,idx):
        return Data(self.idx[idx],self.rec[idx],self.name)

    def merge(self,data):
        """
        return new data, and replaced data by merging self and data
        """
        # check non overlapping cases
        if data.begin() > self.end():
            newdata=self.concatenate(data)
            replaced=Data(np.empty(0),np.empty(0),self.name)
            return newdata,replaced
        elif data.end() < self.begin():
            newdata=data.concatenate(self)
            replaced=Data(np.empty(0),np.empty(0),self.name)
            return newdata,replaced

        # data overlap do full merge
        newrec=[]
        newidx=[]
        oldrec=[]
        oldidx=[]
        ii=0;jj=0
        while ii<len(self.idx) and jj<len(data.idx):
            #print(ii,jj, self.idx[ii], data.idx[jj],newidx)
            if self.idx[ii]==data.idx[jj]:
                #replacing
                newrec.append(data.rec[jj])
                newidx.append(data.idx[jj])
                oldrec.append(self.rec[ii])
                oldidx.append(self.idx[ii])
                ii+=1
                jj+=1
            else:
                if self.idx[ii]<data.idx[jj]:
                    #adding self
                    newrec.append(self.rec[ii])
                    newidx.append(self.idx[ii])
                    ii+=1
                else:
                    #adding data
                    newrec.append(data.rec[jj])
                    newidx.append(data.idx[jj])
                    jj+=1
        while ii<len(self.idx):
            #finishing self
            newrec.append(self.rec[ii])
            newidx.append(self.idx[ii])
            ii+=1
        while jj<len(data.idx):
            #finishing data
            newrec.append(data.rec[jj])
            newidx.append(data.idx[jj])
            jj+=1
        newrec=np.array(newrec)
        newidx=np.array(newidx)
        oldrec=np.array(oldrec)
        oldidx=np.array(oldidx)
        return Data(newidx,newrec,self.name),Data(oldidx,oldrec,self.name)

    def get_size(self):
        return self.rec.nbytes

    def append(self,data):
        self.idx=np.concatenate((self.idx,data.idx))
        self.rec=np.concatenate((self.rec,data.rec))
        return self

    def concatenate(self,data):
        idx=np.concatenate((self.idx,data.idx))
        rec=np.concatenate((self.rec,data.rec))
        return Data(idx,rec,self.name)

    def trim(self,idx1,idx2):
        ii1=np.where( self.idx>=idx1 )[0][0]
        ii2=np.where( self.idx<=idx2 )[0][-1]+1
        return Data(self.idx[ii1:ii2],self.rec[ii1:ii2],self.name)

    def compare(self,data):
        assert len(self)==len(data)
        res=True
        for i1,i2,r1,r2 in zip(self.idx,data.idx,self.rec,data.rec):
            if i1!=i2 or r1!=r2:
                print(f"idx: {i1} {i2}, rec: {r1} {r2}")
                res=False
        return res

    def __add__(self,data):
        if self.name==data.name:
            return self.merge(data)
        else:
            ds=DataSet()
            ds.store_data(self)
            ds.store_data(data)
            return ds

class DataSet:
    def __init__(self,dataset={}):
        self.dataset={}
        self.store(dataset)

    def store(self,dataset):
        for name, data in dataset.items():
            if not Data.is_data(data):
                idx,rec=data
                data=Data(idx,rec,name)
                data.sort()
            self.store_data(data)

    def store_data(self,data):
        if data.name in self:
            data=self.dataset[data.name]
            data,replaced=self.dataset[data.name].merge(data)
        self.dataset[data.name]=data

    def get(self,pattern_or_list,idx1,idx2):
        res ={ self.get_data(name,idx1,idx2) for name in self.search(name) }
        return DataSet(res)

    def get_data(self,name,idx1,idx2):
        return self.dataset[name].trim(idx1,idx2)

    def search(self, pattern_or_list=''):
        if type(pattern_or_list) is str:
            pattern=pattern_or_list
            return [ k for k in self if pattern in k]
        else:
            lst=pattern_or_list
            return [ k for k in lst if k in self]

    def __iter__(self):
        return self.dataset.__iter__()

    def __contains__(self, k):
        return k in self.dataset

    def __getitem__(self,name):
        return self.dataset[data.name]

    def to_dict(self):
        return { data.name: (data.idx,data.rec)}



