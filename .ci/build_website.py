#!/usr/bin/env python3

import argparse
from collections import deque
from distutils.dir_util import copy_tree
from distutils.version import LooseVersion
import git
import json
import os
import requests
import semver
import shutil
import subprocess
import tarfile
import tempfile

websiteGeneratorRepoDir = os.path.abspath(os.environ["SOURCE_PATH"])
hubRepoDir = os.path.abspath(os.environ["POTTER_HUB_PATH"])
controllerRepoDir = os.path.abspath(os.environ["POTTER_CONTROLLER_PATH"])
generatedWebsiteRepoDir = os.path.abspath(os.environ["POTTER_DOCS_PATH"])

parser = argparse.ArgumentParser()
parser.add_argument("--included-releases", dest="includedReleases", type=int, default=3, help="number of releases to include in the docs")
parser.add_argument("--skip-build-and-commit", dest="skipBuildAndCommit", type=bool, default=False, help="skip the hugo build and the subsequent commit of the build output to the $POTTER_DOCS_PATH repo")
parser.add_argument("--include-current-version-only", dest="includeCurrentVersionOnly", type=bool, default=False, help="include only the current local version of $POTTER_HUB_PATH/docs and POTTER_CONTROLLER_PATH/docs. use this flag for generating the docs with local changes.")
args = parser.parse_args()

class HugoClient:
    def __init__(self):
        self.binURL = "https://github.com/gohugoio/hugo/releases/download/v0.78.1/hugo_extended_0.78.1_Linux-64bit.tar.gz"
        self.binPath = "hugo"
        self.sourcePath = f"{websiteGeneratorRepoDir}/hugo"
        self.outPath = f"{generatedWebsiteRepoDir}/docs"
        if not self.isHugoInstalled():
            self.installHugo()
        
        print("sourcePath", self.sourcePath)
        print("outPath", self.outPath)

    def isHugoInstalled(self):
        try:
            command = [self.binPath, "version"]
            result = subprocess.run(command, capture_output=True, text=True)
            print(f"command {command} returned with result: {result}")
            return result.returncode == 0
        except OSError:
            return False

    def installHugo(self):
        tempdir = tempfile.gettempdir()
        print(f"hugo not found in path, installing it to {tempdir}")
        self.binPath = f"{tempdir}/hugo"
        res = requests.get(self.binURL, stream=True)
        with tarfile.open(fileobj=res.raw, mode='r|*') as tar:
            res.raw.seekable = False
            for member in tar:
                if not member.name == "hugo":
                    continue

                fileobj = tar.extractfile(member)
                with open(self.binPath, "wb") as outfile:
                    outfile.write(fileobj.read())
                os.chmod(self.binPath, 744)

    def runBuild(self):
        print("starting hugo build")

        if os.path.exists(self.outPath):
            shutil.rmtree(self.outPath)

        command = [self.binPath, "--source", self.sourcePath, "--destination", self.outPath]
        result = subprocess.run(command, capture_output=True)
        if result.returncode != 0:
            raise Exception(f"website build failed: hugo returned {result}")

def getLatestReleaseTags(gitRepo, numberOfIncludedReleases):
    tags = gitRepo.tags

    # filter out tags that start with "v". these aren't from us but came in from the kubeapps merge
    tags = [tag.name for tag in tags if not tag.name.startswith("v")]
    
    # remove tags where higher patch releases are available
    # [0.0.1, 0.1.0, 0.1.1. 0.1.2, 0.2.0] --> [0.0.1, 0.1.2, 0.2.0]
    tags = removePatchedVersions(tags)

    return tags[-numberOfIncludedReleases:]

def copyDocs(componentName, srcRepoDir):
    print(f"processing component {componentName} in repo {srcRepoDir}")

    gitRepo = git.Repo(srcRepoDir)
    latestReleaseTags = getLatestReleaseTags(gitRepo, args.includedReleases)
    latestReleaseTags.reverse()
    revisions = []
    docsDir = f"{srcRepoDir}/docs"
    first = True

    if args.includeCurrentVersionOnly:
        latestReleaseTags= ["current"]

    for releaseTag in latestReleaseTags:
        print(f"processing version {releaseTag}")

        if not args.includeCurrentVersionOnly:
            gitRepo.git.checkout(releaseTag)

        if not os.path.isdir(docsDir):
            print(f"skip copy: {docsDir} doesn't exist.")
            continue

        if not os.path.isfile(f"{docsDir}/_index.md"):
            print(f"skip copy: {docsDir}/_index.md doesn't exist.")
            continue
        
        if first:
            # latest release must be in special directory
            revision = {
                "version": f"{releaseTag}",
                "dirPath": f"{componentName}-docs",
                "url": f"/{componentName}-docs",
            }
            first = False
        else:
            revision = {
                "version": f"{releaseTag}",
                "dirPath": f"{componentName}-docs-{releaseTag}",
                "url": f"/{componentName}-docs-{releaseTag}",
            }
        revisions.append(revision)

        print(f"copy {docsDir}")
        copy_tree(src=docsDir, dst=f"{websiteGeneratorRepoDir}/hugo/content/{revision['dirPath']}")

    with open(f"{websiteGeneratorRepoDir}/hugo/data/{componentName}-revisions.json", "w") as outfile:
        json.dump(revisions, outfile)

def commitChangesToGeneratedWebsiteRepo():
    print(f"committing changes to {generatedWebsiteRepoDir}")
    generatedWebsiteRepo = git.Repo(generatedWebsiteRepoDir)
    generatedWebsiteRepo.git.add(".")
    generatedWebsiteRepo.git.commit("-m", "updates website")

def removePatchedVersions(tags): 
    tags.sort(key=LooseVersion)
    tags = deque(tags)

    filteredTags = []
    while tags:
        currentTag = tags.popleft()
        currentVer = semver.VersionInfo.parse(currentTag)

        if tags:
            nextTag = tags[0]
            nextVer = semver.VersionInfo.parse(nextTag)

            if not (currentVer.major == nextVer.major and currentVer.minor == nextVer.minor):
                filteredTags.append(currentTag)
        else:
            filteredTags.append(currentTag)
    return filteredTags

# hugo_extended doesn't run on a vanilla Alpine Linux (which is the base image of the CI/CD pipeline containers).
# We therefore must install additional packages when running inside a container of the CI/CD pipeline.
def installAdditionalLinuxPackages():
    print("installing additional packages")
    command = ["apk", "add", "--update", "libc6-compat", "libstdc++"]
    result = subprocess.run(command, capture_output=True, text=True)
    print(f"command {command} returned with result: {result}")
    if result.returncode != 0:
        raise Exception(f"command {command} failed")

def isRunningInCICDPipelineContainer():
    return os.getenv("CONCOURSE_CURRENT_TEAM") != None

if __name__ == "__main__":
    print("starting website build")

    if isRunningInCICDPipelineContainer():
        installAdditionalLinuxPackages()

    copyDocs("hub", hubRepoDir)
    copyDocs("controller", controllerRepoDir)
    if not args.skipBuildAndCommit:
        hugoClient = HugoClient()
        hugoClient.runBuild()
        commitChangesToGeneratedWebsiteRepo()

    print("finished website build")
