import pagestore

def test_version():
    assert len(pagestore.__version__.split('.'))==3

