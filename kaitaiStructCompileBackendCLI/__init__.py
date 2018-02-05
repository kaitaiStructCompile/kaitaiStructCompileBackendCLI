import io
import os
import subprocess
import sys
import tempfile
import typing
from functools import wraps
from pathlib import Path


class Flag:
	slots = ("value",)

	def __init__(self, value):
		self.value = value

	def __hash__(self):
		return hash(self.value)

	def __bool__(self):
		return self.value

	def __repr__(self):
		return self.__class__.__name__ + "(" + repr(self.value) + ")"


langNamespaceCliArgMapping = {
	"python": "python-package",
	"go": "go-package",
	"jre": "java-package",
	"dotNet": "dotnet-namespace",
	"php": "php-namespace",
	"cpp": "cpp-namespace",
	"nim": "nim-module"
}


class paramsRemapping:
	def verbose(v):
		if v:
			return {"--verbose": ",".join(v)}
		else:
			return {}

	def opaqueTypes(v):
		return {"--opaque-types": str(v).lower()}

	def autoRead(v):
		return {"--no-auto-read": Flag(not v)}

	def readStoresPos(v):
		return {"--read-pos": Flag(v)}

	##########

	def namespaces(v):
		res = {}
		for langName, v in v.items():
			if v is not None:
				res["--" + langNamespaceCliArgMapping[langName]] = v
		return res

	def target(v):
		return {"--target": v}

	def destDir(v):
		return {"--outdir": str(v)}

	def additionalFlags(v):
		if isinstance(v, (list, tuple)):
			f = Flag(True)
			return {kk: f for kk in v}
		else:
			res = {}
			for vk, vv in v.items():
				if hasattr(paramsRemapping, vk):
					res.update(getattr(paramsRemapping, vk)(vv))
				else:
					if vv is None:
						vv = Flag(True)
					res[vk] = vv
			print("additionalFlags res", res)
			return res

	def importPath(v):
		return {"--import-path": str(v)}


def init(ICompilerModule, KaitaiCompilerException, utils, defaults):

	osBinariesNamesTable = {
		"nt": (defaults.compilerName + ".bat"),
		"posix": defaults.compilerName
	}

	class CLIPrefsStorage(ICompilerModule.IPrefsStorage):
		@wraps(ICompilerModule.IPrefsStorage.__init__)
		def __init__(self, **kwargs):
			self._stor = {}
			for argName, v in kwargs.items():
				if v is not None:
					if hasattr(paramsRemapping, argName):
						f = getattr(paramsRemapping, argName)
						self._stor.update(f(v))
					else:
						raise ValueError("`" + argName + "` is not an implemented arg")

		def __iadd__(self, other):
			self._stor.update(other._stor)
			return self

		def __add__(self, other):
			res = self.__class__()
			res += self
			res += other
			return res

		def __repr__(self):
			return self.__class__.__name__ + "<" + " ".join(self()) + ">"

		def __call__(self):
			params = []
			for k, v in self._stor.items():
				if v is not None:
					if not isinstance(v, Flag):
						params.append(k)
						params.append(v)
					else:
						if v:
							params.append(k)

			return params

	class CLICompiler(ICompilerModule.ICompiler):
		def __init__(self, progressCallback=None, dirs=None, namespaces=None, additionalFlags: typing.Iterable[str] = (), importPath=None, verbose=(), opaqueTypes=None, autoRead: bool = None, readStoresPos: bool = None, target: str = "python", **kwargs):
			super().__init__(progressCallback, dirs)

			self.compilerExecutable = self.dirs.bin / osBinariesNamesTable[os.name]

			self.commonFlags = CLIPrefsStorage(additionalFlags=("--ksc-json-output",))  # flags needed for this class to work correctly, though a user is allowed to redefine

			self.commonFlags += CLIPrefsStorage(namespaces=namespaces, additionalFlags=additionalFlags, importPath=importPath, verbose=verbose, opaqueTypes=opaqueTypes, autoRead=autoRead, readStoresPos=readStoresPos, **kwargs)

			if not self.compilerExecutable.exists():
				raise KaitaiCompilerException("Compiler executable " + str(self.compilerExecutable) + " doesn't exist")

		def compile_(self, sourceFilesAbsPaths: typing.Iterable[Path], destDir: Path, additionalFlags: typing.Iterable[str], verbose, opaqueTypes, autoRead: bool, readStoresPos: bool, target: str = "python", needInMemory: bool = False, **kwargs) -> typing.Mapping[str, ICompilerModule.ICompileResult]:
			if destDir is None:
				with tempfile.TemporaryDirectory() as tmpDir:
					return self.compile__(sourceFilesAbsPaths, Path(tmpDir), additionalFlags, verbose, opaqueTypes, autoRead, readStoresPos, target, needInMemory, **kwargs)
			else:
				return self.compile__(sourceFilesAbsPaths, destDir, additionalFlags, verbose, opaqueTypes, autoRead, readStoresPos, target, needInMemory, **kwargs)

		def compile__(self, sourceFilesAbsPaths: typing.Iterable[Path], destDir: Path, additionalFlags: typing.Iterable[str], verbose, opaqueTypes, autoRead: bool, readStoresPos: bool, target: str = "python", needInMemory: bool = False, **kwargs) -> typing.Mapping[str, ICompilerModule.ICompileResult]:
			"""Compiles KS package with kaitai-struct-compiler"""
			print("commonFlags", self.commonFlags)
			params = [str(self.compilerExecutable)]

			params += (self.commonFlags + CLIPrefsStorage(destDir=destDir, additionalFlags=additionalFlags, verbose=verbose, opaqueTypes=opaqueTypes, autoRead=autoRead, readStoresPos=readStoresPos, target=target))()

			params.extend((str(p) for p in sourceFilesAbsPaths))

			with subprocess.Popen(params, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
				with io.TextIOWrapper(proc.stdout) as stdoutPipe:
					msg = stdoutPipe.read()
				proc.wait()
				# print(msg, proc.returncode)

				if proc.returncode or msg.find("Exception in thread") > -1:
					raise KaitaiCompilerException(msg)

			res = utils.json.loads(msg)
			from pprint import pprint

			pprint(res)

			resultModules = {}
			issues = []

			for srcFile, fRes in res.items():
				srcFile = Path(srcFile)

				def processErrors(obj: dict):
					errors = obj.get("errors", ())
					for error in errors:
						issues.append(ICompilerModule.issueFactory(srcFile, error.get("file", None), error.get("message", None), line=error.get("line", None), column=error.get("col", None), path=error.get("path", None)))

				processErrors(fRes)

				if "output" in fRes:
					for packageName, specResult in fRes["output"][target].items():
						processErrors(specResult)

						for fSpecResultDescr in specResult.get("files", ()):
							resultModules[packageName] = ICompilerModule.InFileCompileResult(moduleName=packageName, mainClassName=specResult["topLevelName"], msg=msg, path=destDir / fSpecResultDescr["fileName"])

				firstSpecName = fRes.get("firstSpecName", None)
				if firstSpecName is not None:
					try:
						resultModules[firstSpecName].sourcePath = srcFile
					except KeyError:
						pass

			print(resultModules)
			if issues:
				if len(issues) == 1:
					raise issues[0]

				raise KaitaiCompilerException(issues)
			return resultModules

	return CLICompiler
