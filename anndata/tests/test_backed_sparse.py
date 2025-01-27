from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Literal

import h5py
import numpy as np
import pytest
import zarr
from scipy import sparse

import anndata as ad
from anndata._core.anndata import AnnData
from anndata._core.sparse_dataset import sparse_dataset
from anndata.experimental import read_dispatched
from anndata.tests.helpers import AccessTrackingStore, assert_equal, subset_func

if TYPE_CHECKING:
    from pathlib import Path

    from numpy.typing import ArrayLike

subset_func2 = subset_func


@pytest.fixture(params=["h5ad", "zarr"])
def diskfmt(request):
    return request.param


@pytest.fixture(scope="function")
def ondisk_equivalent_adata(
    tmp_path: Path, diskfmt: Literal["h5ad", "zarr"]
) -> tuple[AnnData, AnnData, AnnData, AnnData]:
    csr_path = tmp_path / f"csr.{diskfmt}"
    csc_path = tmp_path / f"csc.{diskfmt}"
    dense_path = tmp_path / f"dense.{diskfmt}"

    write = lambda x, pth, **kwargs: getattr(x, f"write_{diskfmt}")(pth, **kwargs)

    csr_mem = ad.AnnData(X=sparse.random(50, 50, format="csr", density=0.1))
    csc_mem = ad.AnnData(X=csr_mem.X.tocsc())
    dense_mem = ad.AnnData(X=csr_mem.X.toarray())

    write(csr_mem, csr_path)
    write(csc_mem, csc_path)
    # write(csr_mem, dense_path, as_dense="X")
    write(dense_mem, dense_path)
    if diskfmt == "h5ad":
        csr_disk = ad.read_h5ad(csr_path, backed="r")
        csc_disk = ad.read_h5ad(csc_path, backed="r")
        dense_disk = ad.read_h5ad(dense_path, backed="r")
    else:

        def read_zarr_backed(path):
            path = str(path)

            f = zarr.open(path, mode="r")

            # Read with handling for backwards compat
            def callback(func, elem_name, elem, iospec):
                if iospec.encoding_type == "anndata" or elem_name.endswith("/"):
                    return AnnData(
                        **{k: read_dispatched(v, callback) for k, v in elem.items()}
                    )
                if iospec.encoding_type in {"csc_matrix", "csr_matrix"}:
                    return sparse_dataset(elem)
                return func(elem)

            adata = read_dispatched(f, callback=callback)

            return adata

        csr_disk = read_zarr_backed(csr_path)
        csc_disk = read_zarr_backed(csc_path)
        dense_disk = read_zarr_backed(dense_path)

    return csr_mem, csr_disk, csc_disk, dense_disk


def test_backed_indexing(
    ondisk_equivalent_adata: tuple[AnnData, AnnData, AnnData, AnnData],
    subset_func,
    subset_func2,
):
    csr_mem, csr_disk, csc_disk, dense_disk = ondisk_equivalent_adata

    obs_idx = subset_func(csr_mem.obs_names)
    var_idx = subset_func2(csr_mem.var_names)

    assert_equal(csr_mem[obs_idx, var_idx].X, csr_disk[obs_idx, var_idx].X)
    assert_equal(csr_mem[obs_idx, var_idx].X, csc_disk[obs_idx, var_idx].X)
    assert_equal(csr_mem.X[...], csc_disk.X[...])
    assert_equal(csr_mem[obs_idx, :].X, dense_disk[obs_idx, :].X)
    assert_equal(csr_mem[obs_idx].X, csr_disk[obs_idx].X)
    assert_equal(csr_mem[:, var_idx].X, dense_disk[:, var_idx].X)


# test behavior from https://github.com/scverse/anndata/pull/1233
def test_consecutive_bool(
    ondisk_equivalent_adata: tuple[AnnData, AnnData, AnnData, AnnData],
):
    _, csr_disk, csc_disk, _ = ondisk_equivalent_adata

    randomized_mask = np.zeros(csr_disk.shape[0], dtype=bool)
    inds = np.random.choice(csr_disk.shape[0], 20, replace=False)
    inds.sort()
    for i in range(0, len(inds) - 1, 2):
        randomized_mask[inds[i] : inds[i + 1]] = True

    # non-random indices, with alternating one false and n true
    def make_alternating_mask(n):
        mask_alternating = np.ones(csr_disk.shape[0], dtype=bool)
        for i in range(0, csr_disk.shape[0], n):
            mask_alternating[i] = False
        return mask_alternating

    alternating_mask = make_alternating_mask(10)

    # indexing needs to be on `X` directly to trigger the optimization.
    # `_normalize_indices`, which is used by `AnnData`, converts bools to ints with `np.where`
    assert_equal(
        csr_disk.X[alternating_mask, :], csr_disk.X[np.where(alternating_mask)]
    )
    assert_equal(
        csc_disk.X[:, alternating_mask], csc_disk.X[:, np.where(alternating_mask)[0]]
    )
    assert_equal(csr_disk.X[randomized_mask, :], csr_disk.X[np.where(randomized_mask)])
    assert_equal(
        csc_disk.X[:, randomized_mask], csc_disk.X[:, np.where(randomized_mask)[0]]
    )


@pytest.mark.parametrize(
    ["sparse_format", "append_method"],
    [
        pytest.param(sparse.csr_matrix, sparse.vstack),
        pytest.param(sparse.csc_matrix, sparse.hstack),
    ],
)
def test_dataset_append_memory(
    tmp_path: Path,
    sparse_format: Callable[[ArrayLike], sparse.spmatrix],
    append_method: Callable[[list[sparse.spmatrix]], sparse.spmatrix],
    diskfmt: Literal["h5ad", "zarr"],
):
    path = (
        tmp_path / f"test.{diskfmt.replace('ad', '')}"
    )  # diskfmt is either h5ad or zarr
    a = sparse_format(sparse.random(100, 100))
    b = sparse_format(sparse.random(100, 100))
    if diskfmt == "zarr":
        f = zarr.open_group(path, "a")
    else:
        f = h5py.File(path, "a")
    ad._io.specs.write_elem(f, "mtx", a)
    diskmtx = sparse_dataset(f["mtx"])

    diskmtx.append(b)
    fromdisk = diskmtx.to_memory()

    frommem = append_method([a, b])

    assert_equal(fromdisk, frommem)


@pytest.mark.parametrize(
    ["sparse_format", "append_method"],
    [
        pytest.param(sparse.csr_matrix, sparse.vstack),
        pytest.param(sparse.csc_matrix, sparse.hstack),
    ],
)
def test_dataset_append_disk(
    tmp_path: Path,
    sparse_format: Callable[[ArrayLike], sparse.spmatrix],
    append_method: Callable[[list[sparse.spmatrix]], sparse.spmatrix],
    diskfmt: Literal["h5ad", "zarr"],
):
    path = (
        tmp_path / f"test.{diskfmt.replace('ad', '')}"
    )  # diskfmt is either h5ad or zarr
    a = sparse_format(sparse.random(10, 10))
    b = sparse_format(sparse.random(10, 10))

    if diskfmt == "zarr":
        f = zarr.open_group(path, "a")
    else:
        f = h5py.File(path, "a")
    ad._io.specs.write_elem(f, "a", a)
    ad._io.specs.write_elem(f, "b", b)
    a_disk = sparse_dataset(f["a"])
    b_disk = sparse_dataset(f["b"])

    a_disk.append(b_disk)
    fromdisk = a_disk.to_memory()

    frommem = append_method([a, b])

    assert_equal(fromdisk, frommem)


@pytest.mark.parametrize(
    ["sparse_format"],
    [
        pytest.param(sparse.csr_matrix),
        pytest.param(sparse.csc_matrix),
    ],
)
def test_indptr_cache(
    tmp_path: Path,
    sparse_format: Callable[[ArrayLike], sparse.spmatrix],
):
    path = tmp_path / "test.zarr"  # diskfmt is either h5ad or zarr
    a = sparse_format(sparse.random(10, 10))
    f = zarr.open_group(path, "a")
    ad._io.specs.write_elem(f, "X", a)
    store = AccessTrackingStore(path)
    store.set_key_trackers(["X/indptr"])
    f = zarr.open_group(store, "a")
    a_disk = sparse_dataset(f["X"])
    a_disk[:1]
    a_disk[3:5]
    a_disk[6:7]
    a_disk[8:9]
    assert (
        store.get_access_count("X/indptr") == 2
    )  # one each for .zarray and actual access


@pytest.mark.parametrize(
    ["sparse_format", "a_shape", "b_shape"],
    [
        pytest.param("csr", (100, 100), (100, 200)),
        pytest.param("csc", (100, 100), (200, 100)),
    ],
)
def test_wrong_shape(
    tmp_path: Path,
    sparse_format: Literal["csr", "csc"],
    a_shape: tuple[int, int],
    b_shape: tuple[int, int],
    diskfmt: Literal["h5ad", "zarr"],
):
    path = (
        tmp_path / f"test.{diskfmt.replace('ad', '')}"
    )  # diskfmt is either h5ad or zarr
    a_mem = sparse.random(*a_shape, format=sparse_format)
    b_mem = sparse.random(*b_shape, format=sparse_format)

    if diskfmt == "zarr":
        f = zarr.open_group(path, "a")
    else:
        f = h5py.File(path, "a")

    ad._io.specs.write_elem(f, "a", a_mem)
    ad._io.specs.write_elem(f, "b", b_mem)
    a_disk = sparse_dataset(f["a"])
    b_disk = sparse_dataset(f["b"])

    with pytest.raises(AssertionError):
        a_disk.append(b_disk)


def test_reset_group(tmp_path: Path):
    path = tmp_path / "test.zarr"  # diskfmt is either h5ad or zarr
    base = sparse.random(100, 100, format="csr")

    if diskfmt == "zarr":
        f = zarr.open_group(path, "a")
    else:
        f = h5py.File(path, "a")

    ad._io.specs.write_elem(f, "base", base)
    disk_mtx = sparse_dataset(f["base"])
    with pytest.raises(AttributeError):
        disk_mtx.group = f


def test_wrong_formats(tmp_path: Path, diskfmt: Literal["h5ad", "zarr"]):
    path = (
        tmp_path / f"test.{diskfmt.replace('ad', '')}"
    )  # diskfmt is either h5ad or zarr
    base = sparse.random(100, 100, format="csr")

    if diskfmt == "zarr":
        f = zarr.open_group(path, "a")
    else:
        f = h5py.File(path, "a")

    ad._io.specs.write_elem(f, "base", base)
    disk_mtx = sparse_dataset(f["base"])
    pre_checks = disk_mtx.to_memory()

    with pytest.raises(ValueError):
        disk_mtx.append(sparse.random(100, 100, format="csc"))
    with pytest.raises(ValueError):
        disk_mtx.append(sparse.random(100, 100, format="coo"))
    with pytest.raises(NotImplementedError):
        disk_mtx.append(np.random.random((100, 100)))
    disk_dense = f.create_dataset("dense", data=np.random.random((100, 100)))
    with pytest.raises(NotImplementedError):
        disk_mtx.append(disk_dense)

    post_checks = disk_mtx.to_memory()

    # Check nothing changed
    assert not np.any((pre_checks != post_checks).toarray())


def test_anndata_sparse_compat(tmp_path: Path, diskfmt: Literal["h5ad", "zarr"]):
    path = (
        tmp_path / f"test.{diskfmt.replace('ad', '')}"
    )  # diskfmt is either h5ad or zarr
    base = sparse.random(100, 100, format="csr")

    if diskfmt == "zarr":
        f = zarr.open_group(path, "a")
    else:
        f = h5py.File(path, "a")

    ad._io.specs.write_elem(f, "/", base)
    adata = ad.AnnData(sparse_dataset(f["/"]))
    assert_equal(adata.X, base)


def test_backed_sizeof(
    ondisk_equivalent_adata: tuple[AnnData, AnnData, AnnData, AnnData],
    diskfmt: Literal["h5ad", "zarr"],
):
    csr_mem, csr_disk, csc_disk, _ = ondisk_equivalent_adata

    assert csr_mem.__sizeof__() == csr_disk.__sizeof__(with_disk=True)
    assert csr_mem.__sizeof__() == csc_disk.__sizeof__(with_disk=True)
    assert csr_disk.__sizeof__(with_disk=True) == csc_disk.__sizeof__(with_disk=True)
    assert csr_mem.__sizeof__() > csr_disk.__sizeof__()
    assert csr_mem.__sizeof__() > csc_disk.__sizeof__()
