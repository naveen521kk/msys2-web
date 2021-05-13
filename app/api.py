from fastapi import FastAPI, APIRouter, Request, Response
from fastapi.responses import JSONResponse

from typing import Tuple, Dict, List, Set, Iterable, Union
from .appstate import state, SrcInfoPackage
from .utils import version_is_newer_than

router = APIRouter()


def sort_entries(entries: List[Dict]) -> List[Dict]:
    """Sort packages after their dependencies, if possible"""

    done = []
    todo = sorted(entries, key=lambda e: (len(e["makedepends"]), sorted(e["provides"])))

    while todo:
        to_add = []

        potential = []
        for current in todo:
            for other in todo:
                if current is other:
                    continue
                if current["makedepends"] & other["provides"]:
                    if current["provides"] & other["makedepends"] and \
                            len(current["makedepends"]) <= len(other["makedepends"]):
                        # there is a cycle, break it using the one with fewer makedepends
                        potential.append(current)
                        pass
                    else:
                        break
            else:
                to_add.append(current)

        # if all fails, just select one
        if not to_add:
            if potential:
                to_add.append(potential[0])
            else:
                to_add.append(todo[0])

        assert to_add

        for e in to_add:
            done.append(e)
            todo.remove(e)

    return done


@router.get('/buildqueue')
async def index(request: Request, response: Response) -> Response:
    srcinfos = []

    # packages that should be updated
    for s in state.sources.values():
        for k, p in sorted(s.packages.items()):
            if p.name in state.sourceinfos:
                srcinfo = state.sourceinfos[p.name]
                if not version_is_newer_than(srcinfo.build_version, p.version):
                    continue
                srcinfos.append(srcinfo)

    # packages that are new
    available: Dict[str, List[SrcInfoPackage]] = {}
    for srcinfo in state.sourceinfos.values():
        available.setdefault(srcinfo.pkgname, []).append(srcinfo)
    for s in state.sources.values():
        for p in s.packages.values():
            available.pop(p.name, None)
    for sis in available.values():
        srcinfos.extend(sis)

    def build_key(srcinfo: SrcInfoPackage) -> Tuple[str, str]:
        return (srcinfo.repo_url, srcinfo.repo_path)

    to_build: Dict[Tuple, List[SrcInfoPackage]] = {}
    for srcinfo in srcinfos:
        key = build_key(srcinfo)
        to_build.setdefault(key, []).append(srcinfo)

    db_makedepends: Dict[str, Set[str]] = {}
    db_depends: Dict[str, Set[str]] = {}
    for s in state.sources.values():
        for p in s.packages.values():
            db_makedepends.setdefault(p.name, set()).update(p.makedepends.keys())
            db_depends.setdefault(p.name, set()).update(p.depends.keys())

    def get_transitive_depends(packages: Iterable[str]) -> Set[str]:
        todo = set(packages)
        done = set()
        while todo:
            name = todo.pop()
            if name in done:
                continue
            done.add(name)
            # prefer depends from the GIT packages over the DB
            if name in state.sourceinfos:
                si = state.sourceinfos[name]
                todo.update(si.depends.keys())
            elif name in db_makedepends:
                todo.update(db_depends[name])
        return done

    def get_transitive_makedepends(packages: Iterable[str]) -> Set[str]:
        todo: Set[str] = set()
        for name in packages:
            # prefer depends from the GIT packages over the DB
            if name in state.sourceinfos:
                si = state.sourceinfos[name]
                todo.update(si.depends.keys())
                todo.update(si.makedepends.keys())
            elif name in db_makedepends:
                todo.update(db_depends[name])
                todo.update(db_makedepends[name])

        return get_transitive_depends(todo)

    def srcinfo_has_src(si: SrcInfoPackage) -> bool:
        """If there already is a package with the same base/version in the repo
        we can assume that there exists a source package already
        """

        if si.pkgbase in state.sources:
            src = state.sources[si.pkgbase]
            if si.build_version == src.version:
                return True
        return False

    entries = []
    all_provides: Dict[str, Set[str]] = {}
    repo_mapping = {}
    for srcinfos in to_build.values():
        packages = set()
        provides: Set[str] = set()
        needs_src = False
        for si in srcinfos:
            if not srcinfo_has_src(si):
                needs_src = True
            packages.add(si.pkgname)
            repo_mapping[si.pkgname] = si.repo
            for prov in si.provides:
                provides.add(prov)
                all_provides.setdefault(prov, set()).add(si.pkgname)

        entries.append({
            "repo_url": srcinfos[0].repo_url,
            "repo_path": srcinfos[0].repo_path,
            "version": srcinfos[0].build_version,
            "name": srcinfos[0].pkgbase,
            "source": needs_src,
            "packages": packages,
            "provides": provides | packages,
            "makedepends": get_transitive_makedepends(packages),
        })

    # For all packages in the repo and in the queue: remove them from
    # the provides mapping, since real packages always win over provided ones
    for s in state.sources.values():
        for p in s.packages.values():
            all_provides.pop(p.name, None)
    for srcinfos in to_build.values():
        for si in srcinfos:
            all_provides.pop(si.pkgname, None)

    entries = sort_entries(entries)

    def group_by_repo(sequence: Iterable[str]) -> Dict[str, List]:
        grouped: Dict[str, List] = {}
        for name in sequence:
            grouped.setdefault(repo_mapping[name], []).append(name)
        for key, values in grouped.items():
            grouped[key] = sorted(set(values))
        return grouped

    all_packages: Set[str] = set()
    for e in entries:
        # Replace dependencies on provided names with their providing packages
        makedepends = set()
        assert isinstance(e["makedepends"], set)
        assert isinstance(e["packages"], set)
        for d in e["makedepends"]:
            makedepends.update(all_provides.get(d, set([d])))
        # Only show deps which are known at that point.. so in case of a cycle
        # this will be wrong, but we can't do much about that.
        e["depends"] = group_by_repo(makedepends & all_packages)
        all_packages |= set(e["packages"])
        e["packages"] = group_by_repo(e["packages"])
        del e["makedepends"]
        del e["provides"]

    return JSONResponse(entries)


@router.get('/removals')
async def removals(request: Request, response: Response) -> Response:
    # get all packages in the pacman repo which are no in GIT
    entries = []
    for s in state.sources.values():
        for k, p in s.packages.items():
            if p.name not in state.sourceinfos:
                entries.append({
                    "repo": p.repo,
                    "name": p.name,
                })
    return JSONResponse(entries)


@router.get('/search')
async def search(request: Request, response: Response, query: str = "", qtype: str = "") -> Response:

    if qtype not in ["pkg", "binpkg"]:
        qtype = "pkg"

    parts = query.split()
    res_pkg: List[Dict[str, Union[str, List[str], int]]] = []
    exact = {}
    if not query:
        pass
    elif qtype == "pkg":
        for s in state.sources.values():
            if s.name.lower() == query or s.realname.lower() == query:
                exact = s.get_info()
                continue
            if [p for p in parts if p.lower() in s.name.lower()] == parts:
                res_pkg.append(s.get_info())
    elif qtype == "binpkg":
        for s in state.sources.values():
            for sub in s.packages.values():
                if sub.name.lower() == query or sub.realname.lower() == query:
                    exact = s.get_info()
                    continue
                if [p for p in parts if p.lower() in sub.name.lower()] == parts:
                    res_pkg.append(s.get_info())
    return JSONResponse(
        {
            'query': query,
            'qtype': qtype,
            'results': {
                'exact': exact,
                'other': res_pkg
            }
        }
    )


api = FastAPI(title="MSYS2 Packages API", docs_url="/")
api.include_router(router)
