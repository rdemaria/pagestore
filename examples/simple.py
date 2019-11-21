import numpy as np

from pagestore import Data, PageStore

def mk_data(a,b,n,name):
    idx=np.linspace(a,b,n)
    rec=idx**3
    return Data(idx,rec,name)

data1=mk_data(0,10,11,'a')
data2=mk_data(5,15,11,'a')
data2.rec[:]=33
data3, data4 =data1.merge(data2)


data3, data4 =data1.merge(data2)

db=PageStore("testdb")

db.store_data( mk_data(0.,1.,100,'a') )
db.store_data( mk_data(0.5,1.5,100,'a') )

db.store_data( mk_data(0.,1.,100,'b') )
db.store_data( mk_data(0.5,1.5,100,'b') )


db.delete()
