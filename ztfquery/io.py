#!/usr/bin/env python
#

import os, hashlib, logging, multiprocessing
import sys
import time
import pandas
import requests
import warnings
import numpy as np

LOGIN_URL = "https://irsa.ipac.caltech.edu/account/signon/login.do"

import base64

from configparser import ConfigParser
from astropy.io import fits

from .utils.tools import is_running_from_notebook


_SOURCEDIR = os.path.dirname(os.path.realpath(__file__))


_ENCRYPT_FILE = os.path.expanduser("~") + "/.ztfquery"
_ENCRYPTING_FILE = os.path.expanduser("~") + "/.queryirsa"


LOCALSOURCE = os.getenv("ZTFDATA", "./Data/")
CCIN2P3_SOURCE = "/sps/ztf/data/"

logger = logging.getLogger(__name__)


# ================= #
#  High level tools #
# ================= #
def get_file(
    filename,
    suffix=None,
    session=None,
    downloadit=True,
    check_suffix=True,
    dlfrom="irsa",
    overwrite=False,
    maxnprocess=4,
    exist=True,
    test_file=True,
    squeeze=True,
    show_progress=True,
    client=None,
    wait=None,
    fill_notexist="None",
    **kwargs,
):
    """Get full path associate to the filename.
    If you don't have it on your computer, this downloads it for you.

    Parameters
    ----------
    filename: str
        name of the file you want.
        raw, cal and sci filenames are accepted.

    suffix: str
        actual suffix of the file you want. By default it is that of the filename
        but you can request associated files with other suffix.
        For instance:
        if filename = ztf_20190917468333_000698_zi_c03_o_q2_sciimg.fits and suffix='mskimg.fits'
        you will be looking for ztf_20190917468333_000698_zi_c03_o_q2_mskimg.fits.
        - If you input a raw file, then suffix is used as 'imgtypecode'

    session: requests.Session
        a request session you already have (if any)

    downloadit: bool
        If you do not have the file locally, shall this download it for you ?

    wait: Nonen str, float
        Waiting time for dwnloaded the file. It helps mostly when dask is massively multi-downloading.
        - None: if Client, wait -> len(to_be_downloaded)/100 (so 100 per second) ; corresponds to wait="100"
        - "None": no limit
        - "float_in_string": waiting time reported to the size of file to download: len(to_be_downloaded)/wait
        - float: absolute time to wait.

    **kwargs goes to download_from_filename

    Returns
    -------
    fullpath (or None if not data)

    """
    from .buildurl import filename_to_url

    local_filenames = np.asarray(
        [
            filename_to_url(
                filename_,
                suffix=suffix_,
                source="local",
                check_suffix=check_suffix,
            )
            for filename_ in np.atleast_1d(filename)
            for suffix_ in np.atleast_1d(suffix)
        ]
    )
    if not exist:
        return local_filenames

    # local_filenames
    if overwrite:
        flag_todl = np.asarray( np.ones( len(local_filenames)), dtype="bool")
    else:
        flag_todl = np.asarray(
            [
                (not os.path.isfile(f_))
                or (test_file and ".fits" in f_ and _is_fitsfile_bad_(f_))
                for f_ in local_filenames
            ]
        )

    # DL if needed (and wanted)
    if np.any(flag_todl) and downloadit:
        if client is not None and wait is None:
            wait = "100"
        if type(wait) is str:
            if wait == "None":
                wait = None
            else:
                try:
                    wait = len(local_filenames[flag_todl]) / float(wait)
                except:
                    warnings.warn(
                        f"cannot parse the inpout waiting time {wait} -> None used."
                    )
                    wait = None

        f_ = download_from_filename(
            local_filenames[flag_todl],
            session=session,
            show_progress=show_progress,
            host=dlfrom,
            overwrite=True,
            maxnprocess=maxnprocess,
            client=client,
            wait=wait,
            check_suffix=check_suffix,
        )
        if client is not None:
            from dask.distributed import wait as dwait
            _ = dwait(f_)

    # - Output
    if fill_notexist == "remove":
        local_filenames = [f for f in local_filenames if os.path.isfile(f)]

    elif fill_notexist != "None":
        local_filenames = [
            f if os.path.isfile(f) else fill_notexist for f in local_filenames
        ]

    if len(local_filenames) == 1 and squeeze:
        return local_filenames[0]

    return local_filenames


def filefracday_to_local_rawdata(filefracday, ccdid="*"):
    """ """
    from glob import glob

    rawfilename = filefracday_and_ccdid_to_rawfilename(filefracday, ccdid=ccdid)
    return np.sort(glob(rawfilename))


def filefracday_and_ccdid_to_rawfilename(filefracday, ccdid, source="local"):
    from .buildurl import filefrac_to_year_monthday_fracday, _source_to_location_

    filefracday = str(filefracday)
    year, month, day, fracday = filefrac_to_year_monthday_fracday(filefracday)
    source = _source_to_location_(source)
    cstring = "*" if ccdid in ["*", "all"] else f"_c{ccdid:02d}_"
    return os.path.join(
        source,
        "raw",
        year,
        f"{month}{day}",
        fracday,
        f"ztf_{filefracday}*{cstring}*.fits.fz",
    )


def bulk_get_file(filenames, client=None, suffix=None,
                    as_dask="delayed", **kwargs):
    """
    Parameters
    ----------
    as_dask: str
         could be:
         - delayed
         - futures
         - gathered
         - computed
    
    Return
    ------
    list
        - computed or gathered option will return the list of downloaded filepath
          -> list_of_file
        - delayed of futures will return in addition the session that should eventually be closed.
          -> list_of_file, session
    """
    import dask

    session = open_irsa_session()

    if client is None and as_dask in ["gather", "gathered"]:
        as_dask = "compute"
        
    d_files = [ dask.delayed(get_file)(filename, suffix=suffix, session=session,
                                show_progress=False, maxnprocess=1,**kwargs)
                for filename in filenames]
        
    if as_dask == "delayed":
        return d_files, session
    
    if as_dask in ["computed", "compute"]:
        results = dask.delayed(list)(d_files).compute()
        session.close()
        return results

    if as_dask == "persist":
        if client is not None:
            persisted = client.persist(d_files)
        else:
            persisted = [f_.persist() for f_ in d_files]
        return persisted, session

    futures = client.compute(d_files)
    if as_dask == "futures":
        return futures, session
    if as_dask in ["gather", "gathered"]:
        gathered = client.gather(futures)
        session.close()
        return gathered
    
    raise ValueError(f"Cannot parse the given as_dask {as_dask}")


def get_filedataframe(filenames):
    """get a dataframe of the files"""
    import pandas

    fileserie = pandas.Series(filenames, name="filename")

    fdata = pandas.DataFrame.from_records(fileserie.apply(parse_filename))
    fdata["isfile"] = fileserie.apply(os.path.isfile)
    merged = fdata.merge(fileserie, left_index=True, right_index=True)
    return merged


def filename_to_kind(filename):
    """ get the kind given the filename
    
    this using buildurl.filename_to_kind()

    Parameters
    ----------
    filename: str or list
        (list of) filename or basename

    Returns
    -------
    str or list
        kind (sci, raw, cal)

    """
    from .buildurl import filename_to_kind
    if type(filename) is str:
        return filename_to_kind(filename)
    return [filename_to_kind(filename_) for filename_ in filename]
        
    
def parse_filename(filename, as_serie=True):
    """ """
    from .buildurl import parse_filename

    parsed = parse_filename(filename)
    if as_serie:
        return pandas.Series(parsed)
    
    return parsed


def get_mask_of_filename(filename):
    """ build the name of the """
    from ztfquery import get_file
    
    filepath_mask = dask.delayed(io.get_file)(filename, suffix="mskimg.fits",
                                                  downloadit=download,
                                                  show_progress=False, maxnprocess=1,
                                                  **kwargs)








def download_from_filename(
    filename,
    session=None,
    suffix=None,
    overwrite=False,
    auth=None,
    nodl=False,
    host="irsa",
    maxnprocess=4,
    show_progress=True,
    check_suffix=True,
    client=None,
    wait=None,
    **kwargs,
    ):
    """ Download the file associated to the given filename 

    Parameters
    ----------
    session: requests.Session
        session used to call the get method


    """
    if host not in ["irsa", "ccin2p3"]:
        raise ValueError(f"Only 'irsa' and 'ccin2p3' host implemented: {host} given")

    from .buildurl import filename_to_url

    remote_filename = []
    local_filename = []
    for file_ in np.atleast_1d(filename):
        remote_filename.append(
            filename_to_url(
                file_,
                suffix=suffix,
                source=host,
                check_suffix=check_suffix,
            )
        )
        local_filename.append(
            filename_to_url(
                file_,
                suffix=suffix,
                source="local",
                check_suffix=check_suffix,
            )
        )

    if nodl:
        return [remote_filename, local_filename]

    nprocess = np.min([maxnprocess, len(local_filename)])
    # cookies:
    if auth is None:
        auth = None, None # no username, no password
    cookies = get_cookie(*auth, session=session, update=False)
    
    return download_url(
            remote_filename,
            local_filename,
            session=session,
            nprocess=nprocess,
            client=client,
            wait=wait,
            overwrite=overwrite,
            cookies=cookies,
            show_progress=show_progress,
            **kwargs)


def _parse_filename_(filename, builddir=False, squeeze=True, exists=False):
    """ """
    from glob import glob

    directory = os.path.dirname(filename)
    # basename  = os.path.basename(filename).split(".")[0]
    # extension = filename.split(".")[-1]

    if builddir:
        oldmask = os.umask(0o002)
        os.makedirs(directory, exist_ok=True)

    # unique object
    if "*" in filename and not exists:
        logger.warning(
            "apparent variable conflict, filename contains '*', but exists is False."
        )

    localfile = glob(filename) if exists else np.atleast_1d(filename)
    if squeeze:
        if len(localfile) == 0:
            return None
        if len(localfile) == 1:
            return localfile[0]

    return localfile

def open_irsa_session(auth=None, incl_cookies=True):
    """ open a session that has irsa cookies 

    Parameters
    ----------
    auth: list
        irsa logging (id, pwd)

    incl_cookies: bool
        should the session acquire irsa login cookies ?

    Returns
    -------
    requests.Session
    """
    session = requests.Session()

    if incl_cookies:
        if auth is None:
            auth = None, None # no username, no password
        _ = get_cookie(*auth, session, update=True) # update=True is useless most likely, but safer

    return session

# ================= #
#  Crypting         #
# ================= #
def _load_id_(which, askit=True, token_based=False):
    """returns login information for the requested enty"""
    import base64

    config = ConfigParser()
    config.read(_ENCRYPT_FILE)

    if which in ["fritz"]:
        token_based = True

    if which not in config.sections():
        if not askit:
            raise AttributeError(
                f"No {which} account setup. Add then in .ztfquery or run ztfquery.io.set_account({which})"
            )
        else:
            logger.warning(f"No {which} account setup, please provide it")
            set_account(which, token_based=token_based)
            config = ConfigParser()
            config.read(_ENCRYPT_FILE)

    if not token_based:
        return config[which.lower()]["username"], base64.b64decode(
            config[which.lower()]["password"][2:-1]
        ).decode("utf-8")

    return base64.b64decode(config[which.lower()]["token"][2:-1]).decode("utf-8")


def set_account(
    which,
    username=None,
    password=None,
    token=None,
    test=True,
    force=False,
    token_based=False,
    no_user=False,
    ):
    """Setup the username and password (simply encrypted!) for the given `which` account.
    Saved in ~/.ztfquery
    """
    import base64
    import getpass

    config = ConfigParser()
    config.read(_ENCRYPT_FILE)

    if which in ["fritz"]:
        token_based = True

    if which in ["logs"]:
        no_user = True

    if token_based:
        if token is None:
            token = input(f"Enter your {which} token:")
    else:
        # - Name & Password
        if username is None:
            if no_user:
                username = "None"
            else:
                username = input(f"Enter your {which} login: ")

        if password is None:
            password = getpass.getpass()

    #
    # -> Starting tests
    if test:
        wrong_ = False
        if which == "irsa":
            if not test_irsa_account([username, password]):
                warnings.warn(
                    "The irsa_test for you account returns False. Most likely you provided incorrect logins"
                )
                wrong_ = True
        else:
            if not token_based:
                logger.info(
                    f"No test designed for {which}. Cannot test if logins are correct."
                )
            else:
                logger.info(
                    f"No test designed for {which}. Cannot test if token is correct."
                )

        if wrong_ and not force:
            if not token_based:
                raise ValueError(
                    f"Bad username/passworg for {which}. force=False so the logins are not stored. "
                )
            else:
                raise ValueError(
                    "Bad token for {which}. force=False so the logins are not stored."
                )
    # <- end of tests
    #
    if not token_based:
        password_ = base64.b64encode(password.encode("utf-8"))
        config[which.lower()] = {"username": username, "password": password_}
    else:
        token_ = base64.b64encode(token.encode("utf-8"))
        config[which.lower()] = {"token": token_}

    with open(_ENCRYPT_FILE, "w") as configfile:
        config.write(configfile)


#
# TEST
#
# - Password testing
def test_irsa_account(auth=None, **kwargs):
    """returns True if the IRSA account is correctly set."""
    if auth is None:
        auth = _load_id_("irsa")
    return ".ipac.caltech.edu" in get_cookie(*auth, **kwargs)._cookies

# - File testing
def get_localfiles(extension="*", startpath=None):
    """Look for all file with the given extension recursively starting from `startpath`.
    (based on glob)

    Parameters
    ----------
    extension: [string] -optional-
        All the 'file.{}'.format(extension) will be looked at.
        (first '.' ignored such that extension='.fits' is equivalent to extension='fits')

    startpath: [None or path] -optional-
        From which directory does this start to look at.
        If None: $ZTFDATA (stored as io.LOCALSOURCE) will be used.

    Returns
    -------
    list of file.
    """
    from glob import glob

    if startpath is None:
        startpath = LOCALSOURCE
    if extension.startswith("."):
        extension = extension[1:]

    return [f for f in glob(startpath + f"**/*.{extension}", recursive=True)]


def run_full_filecheck(
    extension="*",
    startpath=None,
    erasebad=True,
    redownload=False,
    nprocess=4,
    show_progress=True,
    **kwargs,
):
    """Look for all file with the given extension recursively starting from `startpath` and checks if the file
    is usable ok not. This returns the bad files.

    Parameters
    ----------

    // Data files
    extension: [string] -optional-
        All the 'file.{}'.format(extension) will be looked at.
        (first '.' ignored such that extension='.fits' is equivalent to extension='fits')

    startpath: [None or path] -optional-
        From which directory does this start to look at.
        If None: $ZTFDATA (stored as io.LOCALSOURCE) will be used.

    // Check options

    erasebad: [bool] -optional-
        Do you want to remove from your local directory the corrupted files ?

    redownload: [bool] -optional-
        Shall corrupted file be automatically re downloaded ?
        (Only works for IRSA files ('/sci/','/raw/', '/ref/', '/cal/')

    nprocess: [int] -optional-
        Number of paralell processing

    show_progress: [bool] -optional-
        Do you want to show the progress bar ?

    Returns
    -------
    list of corrupted/bad files (might already be removed, see erasebad)

    """
    all_ztffiles = get_localfiles(extension=extension, startpath=startpath)
    logger.info(f"{len(all_ztffiles)} files to check")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        badfiles = test_files(
            all_ztffiles,
            erasebad=erasebad,
            nprocess=nprocess,
            show_progress=show_progress,
            redownload=redownload,
            **kwargs,
        )

    return badfiles


def test_files(
    filename,
    erasebad=True,
    nprocess=1,
    show_progress=True,
    redownload=False,
    **kwargs,
    ):
    """

    Parameters
    ----------
    filename: [fiulepath or list of]
        File(s) to be checked.

    erasebad: [bool] -optional-
        Do you want to remove from your local directory the corrupted files ?

    redownload: [bool] -optional-
        Shall corrupted file be automatically re downloaded ?
        (Only works for IRSA files ('/sci/','/raw/', '/ref/', '/cal/')

    nprocess: [int] -optional-
        Number of paralell processing

    show_progress: [bool] -optional-
        Do you want to show the progress bar ?

    **kwargs goes to _test_file_: write_hask

    Returns
    -------
    list of corrupted/bad files (might already be removed, see erasebad)

    """
    if nprocess is None:
        nprocess = 1
    elif nprocess < 1:
        raise ValueError("nprocess must 1 or higher (None means 1)")

    filename = np.atleast_1d(filename)

    if nprocess == 1:
        fileissue = [
            f
            for f in filename
            if not _test_file_(f, erasebad=erasebad, redownload=redownload, **kwargs)
        ]
    else:
        import multiprocessing

        if show_progress:
            from astropy.utils.console import ProgressBar

            bar = ProgressBar(len(filename), ipython_widget=is_running_from_notebook())
        else:
            bar = None

        erasebad_ = [erasebad] * len(filename)
        fileissue = []

        with multiprocessing.Pool(nprocess) as p:
            # Da Loop
            for j, isgood in enumerate(
                p.imap(_test_file_multiprocess_, zip(filename, erasebad_))
            ):
                if bar is not None:
                    bar.update(j)
                if not isgood:
                    fileissue.append(filename[j])

            if bar is not None:
                bar.update(len(filename))

    if len(fileissue) > 0:
        logger.info(f"{len(fileissue)} file failed")
        if redownload:
            from .buildurl import _localsource_to_source_

            to_download_urls, locations = np.asarray(
                [_localsource_to_source_(filename) for filename in fileissue]
            ).T
            source_to_dl = ["irsa"]
            for source in source_to_dl:
                source_dl = np.in1d(locations, [source])
                logger.info(
                    f"Downloading {len(source_dl[source_dl])} files from {source}"
                )
                download_url(
                    np.asarray(to_download_urls)[source_dl],
                    np.asarray(fileissue)[source_dl],
                    show_progress=show_progress,
                    session=session,
                    overwrite=True,
                    nprocess=nprocess,
                    cookies=get_cookie(*_load_id_(source),
                                       session=session, update=False),
                    **kwargs,
                )
            for source_ in np.unique(locations):
                if source_ is not None and source_ not in source_to_dl:
                    logger.warning(
                        f"files from {source_} have not downloaded (not implemented)."
                    )

        return fileissue


def _test_file_multiprocess_(args):
    """ """
    filename, erasebad = args
    return _test_file_(filename, erasebad=erasebad, fromdl=False, redownload=False)


def _are_fitsfiles_bad_(filenames, test_exist=True):
    """ """
    return [_is_fitsfile_bad_(f_, test_exist=test_exist) for f_ in filenames]


def _is_fitsfile_bad_(filename, test_exist=True):
    """ """
    if not os.path.isfile(filename):
        return test_exist

    try:
        _ = fits.getdata(filename)
        return False
    except:
        return True

def _is_textfile_bad_(filename):
    """ """
    try:
        _ = open(filename).read().splitlines()
        return False
    except:
        return True


def _test_file_(filename, erasebad=True, fromdl=False, redownload=False, write_hash=False):
    """ """
    propissue = dict(erasebad=erasebad, fromdl=fromdl, redownload=redownload)

    if ".fits" in filename:
        if not hash_for_file_exists(filename):
            try:
                _ = fits.getdata(filename)
                if write_hash:
                    calculate_and_write_hash(filename)
                else:
                    calculate_hash(filename)
                    
            except FileNotFoundError:
                logger.debug(f"[Errno 2] No such file or directory: {filename}")
            except:
                _fileissue_(filename, **propissue)
                return False

    elif ".txt" in filename:
        if not hash_for_file_exists(filename):
            try:
                _ = open(filename).read().splitlines()
                if write_hash:
                    calculate_and_write_hash(filename)
                else:
                    calculate_hash(filename)
            except FileNotFoundError:
                logger.debug(f"[Errno 2] No such file or directory: {filename}")
            except:
                _fileissue_(filename, **propissue)
                return False

    # other extensions
    else:
        logger.warning("no file testing made for .%s files" % filename.split(".")[-1])

    return True


def _fileissue_(filename, erasebad=True, fromdl=False, redownload=False):
    """ """
    if fromdl:
        logger.info(f"Download failed {filename} seems corrupted (cannot open)")
    else:
        logger.info(f"cannot open file {filename}")

    if erasebad:
        logger.info(f"removing {filename}")
        os.remove(filename)
    else:
        logger.info(f"{filename} NOT ERASED")

    if redownload:
        from .buildurl import _localsource_to_source_

        url_to_dl, location = _localsource_to_source_(filename)
        if url_to_dl is not None:
            download_single_url(
                url_to_dl,
                fileout=filename,
                overwrite=True,
                cookies=get_cookie(*_load_id_(location)),
            )
        else:
            logger.info("No url to donwload, redownload ignored")


# ================= #
#   Logging Tools   #
# ================= #
def get_cookie(username=None, password=None, session=None, update=False):
    """ get a cookie from the IPAC login service

    Parameters
    ----------
    username: str, None
        The IPAC account username. 
        If None given, this will prompt you for one or will use already stored one.

    password: str, None
        The IPAC account password
        If None given, this will prompt you for one or will use already stored one.

    session: requests.Session
        session used to call the get method

    update: bool
        if a session is given and if this session already has cookies
        should this update it ? 
        If not, then nothing happens here.
    """
    # has a session that has cookies, should this update ?
    if session is not None and len(session.cookies)>0 and not update:
        return

    if username is None or password is None:
        username, password = _load_id_("irsa")
    
    url = "%s?josso_cmd=login&josso_username=%s&josso_password=%s" % (
        LOGIN_URL,
        username,
        password,
    )
    if session is not None:
        _ = session.get(url) # this attach the cookies to the session
        
    return requests.get(url).cookies # the returns the cookies


def _download_(args):
    """To be used within _ZTFDownloader_.download_data()
    url, fileout,overwrite = args
    """
    url, fileout, session, overwrite, wait, cutouts, ra, dec, cutout_size = args

    download_single_url(
        url,
        fileout=fileout,
        session=session,
        overwrite=overwrite,
        wait=wait,
        cutouts=cutouts,
        radec=[ra, dec],
        cutout_size=cutout_size,
    )


def download_url(
    to_download_urls,
    download_location,
    session=None,
    cutouts=False,
    show_progress=True,
    wait=None,
    overwrite=False,
    nprocess=None,
    cookies=None,
    client=None,
    radec=None,
    cutout_size=None,
    verbose=False,
    pool=None,
    **kwargs,
    ):
    """ """
    #
    # - Dask Client
    if client is not None:
        from dask import delayed

        d_download = [
            delayed(download_single_url)(
                url,
                cutouts=cutouts,
                session=session,                
                fileout=fileout,
                show_progress=False,
                overwrite=overwrite,
                wait=wait,
                cookies=cookies,
                radec=radec,
                cutout_size=cutout_size,
                **kwargs
            )
            for url, fileout in zip(to_download_urls, download_location)
        ]
        return client.compute(d_download)

    #
    # - MultiProcessing (or not)
    if nprocess is None:
        nprocess = 1
        
    elif nprocess < 1:
        raise ValueError("nprocess must 1 or higher (None means 1)")

    if nprocess == 1:
        # Single processing
        logger.debug("No parallel downloading")
        for url, fileout in zip(to_download_urls, download_location):
            download_single_url(
                url,
                cutouts=cutouts,
                fileout=fileout,
                session=session,
                show_progress=show_progress,
                overwrite=overwrite,
                cookies=cookies,
                radec=radec,
                cutout_size=cutout_size,
                wait=wait,
                **kwargs,
            )

    else:
        # Multi processing
        if show_progress:
            from astropy.utils.console import ProgressBar

            bar = ProgressBar(
                len(to_download_urls), ipython_widget=is_running_from_notebook()
            )
        else:
            bar = None

        logger.debug("parallel downloading ; asking for %d processes" % nprocess)

        # Passing arguments
        overwrite_ = [overwrite] * len(to_download_urls)
        wait_ = [wait] * len(to_download_urls)
        cutouts_ = [cutouts] * len(to_download_urls)
        session = [session] * len(to_download_urls)
        if radec is  None:
            radec = [None,None]
        
        ra_ = [radec[0]] * len(to_download_urls)
        dec_ = [radec[1]] * len(to_download_urls)
        cutout_size_ = [cutout_size] * len(to_download_urls)

        args = zip(
            to_download_urls,
            download_location,
            session,
            overwrite_,
            wait_,
            cutouts_,
            ra_,
            dec_,
            cutout_size_,
        )
        if pool is None:
            close_pool = True
            pool = multiprocessing.Pool(nprocess)
        else:
            close_pool = False
            
        # Da Loop
        for j, result in enumerate( pool.imap_unordered(_download_, args) ):
            if bar is not None:
                bar.update(j)
                
        if close_pool:
            pool.close()
            
def download_fitsdata(url, session=None, **kwargs):
    """ download a fitsfile and get the first data (nothing stored) 
    
    Parameters
    ----------
    url: str
        fits file url.

    session: requests.Session
        session that already has the cookies.

    **kwargs goes to download_single_url
    
    Return
    ------
    astropy.HDUList    
    """
    
    out = download_single_url(url, session=session, **kwargs)
    return fits.HDUList.fromstring(out.content)

def download_single_url(
    url,
    session=None,
    cutouts=False,
    radec=None,
    cutout_size=30,
    fileout=None,
    overwrite=False,
    cookies=None,
    show_progress=True,
    chunk=1024,
    wait=None,
    randomize_wait=True,
    filecheck=True,
    erasebad=True,
    write_hash=False,
    **kwargs,
    ):
    """Download the url target using requests.get.
    the data is returned (if fileout is None) or stored in `fileout`
    """
    
    if wait is not None:
        waiting = wait if not randomize_wait else np.random.uniform(0, wait)
        time.sleep(waiting)

    if fileout is not None and not overwrite and os.path.isfile(fileout):
        logger.debug(f"{fileout} already exists: skipped")
        return
    else:
        if fileout:
            logger.debug(f"downloading {url} to {fileout}")

    if cutouts:
        if radec is None:
            raise ValueError(
                "You selected to download cutouts only. Please provide the radec parameter. Default cutout_size: 30 arcsec"
            )
        url += f"?center={radec[0]},{radec[1]}&size={cutout_size}arcsec&gzip=false"

    # = Password and Username
    if cookies is None:
        # this attach the cookies to the session if given
        # and the returned 'cookies' is None
        cookies = get_cookie(session=session, update=False) 
         
    # - requests options
    download_prop = {**dict(stream=True), **kwargs}
    if cookies not in ["no_cookies"] and cookies is not None: # add cookies if needed
        download_prop["cookies"] = cookies

    request_fnc = "get" if not "data" in download_prop else "post"
    requests_or_session = requests if session is None else session
    
    # = Where should the data be saved?
    if fileout is not None:
        directory = os.path.dirname(fileout)
        oldmask = os.umask(0o002)

        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)

    else:
        download_prop["stream"] = False
        return getattr(requests_or_session, request_fnc)(url, **download_prop)

    # With Progress bar?
    if not show_progress:
        response = getattr(requests_or_session, request_fnc)(url, **download_prop)
        if response.status_code == 200:
            with open(fileout, "wb") as f:
                for data in response.iter_content(chunk):
                    f.write(data)

    else:
        from astropy.utils.console import ProgressBar

        response = getattr(requests_or_session, request_fnc)(url, **download_prop)
        if response.status_code == 200:
            chunk_barstep = 500
            f = open(fileout, "wb")
            with ProgressBar(
                int(response.headers.get("content-length")) / (chunk_barstep * chunk),
                ipython_widget=is_running_from_notebook(),
            ) as bar:
                for i, data in enumerate(response.iter_content(chunk_size=chunk)):
                    if i % chunk_barstep == 0:
                        bar.update()
                    f.write(data)
            f.close()

    if filecheck:
        _test_file_(fileout, erasebad=erasebad, fromdl=True, write_hash=write_hash)

def test_url_exists(filename_url, session=None,cookies=None, **kwargs): 
    """ 
    Test if the file associated to the given url exist on remote IRSA server.

    Parameters
    ----------
    filename_url : str 
        IRSA url from `ztfquery.buildurl.filename_to_url` for example

    session: requests.Session
        session used to call the get method

    Returns
    -------
        bool
        True if file exists. False if status_code differs from 200.    
    """
    
    # = Password and Username
    if cookies is None:
        # this attach the cookies to the session if given
        # and the returned 'cookies' is None
        cookies = get_cookie(session=session, update=False) 
         
    # - requests options
    download_prop = {**dict(stream=False), **kwargs}
    if cookies not in ["no_cookies"] and cookies is not None: # add cookies if needed
        download_prop["cookies"] = cookies

    request_fnc = "head" 
    requests_or_session = requests if session is None else session

    response = getattr(requests_or_session, request_fnc)(filename_url, **download_prop)
    if response.status_code == 200 : 
        return True
    else : 
        return False

# =============== #
#                 #
#  HASH tools     #
#                 #
# =============== #

def calculate_hash(fname):
    """ """
    f = open(fname, "rb")
    hash_md5 = hashlib.md5()
    for chunk in iter(lambda: f.read(4096), b""):
        hash_md5.update(chunk)
    hexdigest = hash_md5.hexdigest()
    f.close()
    return hexdigest

def calculate_and_write_hash(fname):
    """ """
    hexdigest = calculate_hash(fname)
    f_hash = open(f"{fname}.md5", "w")
    f_hash.write(hexdigest)
    f_hash.close()

def read_hash(fname):
    """ """
    f_hash = open(f"{fname}.md5", "r")
    hash_md5 = f_hash.read()
    return hash_md5

def compare_hash(fname):
    """ """
    f_hash = open(hash_fname, "r")
    hash_md5_read = f_hash.read()
    hash_md5_calculated = calculate_hash(fname)
    if hash_md5_read == hash_md5_calculated:
        return True
    else:
        return False

def hash_for_file_exists(fname):
    """ """
    return os.path.exists(f"{fname}.md5")
