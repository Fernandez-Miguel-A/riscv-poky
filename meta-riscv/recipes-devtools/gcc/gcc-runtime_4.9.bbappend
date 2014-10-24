FILESEXTRAPATHS_prepend := "${THISDIR}/files:"
SRC_URI += "file://gcc-4.9-riscv.patch"

EXTRA_OECONF += "--disable-libmudflap \
                --disable-libssp \
                --disable-libquadmath \
                --disable-nls \
                --enable-tls \
                --disable-multilib"
