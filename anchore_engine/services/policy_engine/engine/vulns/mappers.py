import copy
import datetime
import re
import uuid
from typing import Dict, List, Optional

from anchore_engine.clients.grype_wrapper import GrypeVulnerabilityMetadata
from anchore_engine.common import nonos_package_types
from anchore_engine.common.models.policy_engine import (
    CVSS,
    Advisory,
    Artifact,
    FixedArtifact,
    Match,
    NVDReference,
    Vulnerability,
    VulnerabilityMatch,
)
from anchore_engine.db import Image
from anchore_engine.services.policy_engine.engine.vulns.utils import get_api_endpoint
from anchore_engine.subsys import logger as log
from anchore_engine.util.cpe_generators import (
    generate_fuzzy_cpes,
    generate_fuzzy_go_cpes,
    generate_java_cpes,
)
from anchore_engine.util.rpm import split_fullversion


class DistroMapper:
    engine_distro = None
    grype_os = None
    grype_like_os = None

    def __init__(self, engine_distro, grype_os, grype_like_os):
        self.engine_distro = engine_distro
        self.grype_os = grype_os
        self.grype_like_os = grype_like_os

    def to_grype_distro(self, version):
        return {
            "name": self.grype_os,
            "version": version,
            "idLike": self.grype_like_os,
        }


class PackageMapper:
    def __init__(self, engine_type, grype_type, grype_language=None):
        self.engine_type = engine_type
        self.grype_type = grype_type
        # default language to blank string
        self.grype_language = grype_language if grype_language else ""

    def image_content_to_grype_sbom(self, record: Dict):
        """
        Creates a grype sbom formatted record from a single image content API response record

        Refer to specific mappers for example input
        """

        artifact = {
            "id": str(uuid.uuid4()),
            "name": record.get("package"),
            "version": record.get("version"),
            "type": self.grype_type,
            "cpes": record.get("cpes", [])
            # Package type specific mappers add metadata attribute
        }

        return artifact


class KBMapper(PackageMapper):
    """
    Package Mapper for microsoft KBs.
    """

    def __init__(self):
        super(KBMapper, self).__init__(engine_type="kb", grype_type="msrc-kb")

    def image_content_to_grype_sbom(self, record: Dict):
        artifact = {
            "id": str(uuid.uuid4()),
            "name": record.get("sourcepkg"),
            "version": record.get("version"),
            "type": self.grype_type,
            "locations": [{"path": "registry"}],
        }
        return artifact


class LinuxDistroPackageMapper(PackageMapper):
    def image_content_to_grype_sbom(self, record: Dict):
        """
        Initializes grype sbom artifact and sets the default location to pkgdb - base for rpm, dpkg and apkg types
        """
        artifact = super().image_content_to_grype_sbom(record)
        artifact["locations"] = [{"path": "pkgdb"}]
        return artifact


class RpmMapper(LinuxDistroPackageMapper):
    def __init__(self):
        super(RpmMapper, self).__init__(engine_type="rpm", grype_type="rpm")

    def image_content_to_grype_sbom(self, record: Dict):
        """
        Adds the source package information to grype sbom

        Source package has been through a transformation before this point - from syft sbom to analyzer manifest
        in anchore_engine/analyzers/syft/handlers/rpm.py. Fortunately the value remains unchanged and does not require
        additional processing

        """
        artifact = super().image_content_to_grype_sbom(record)

        # Handle Epoch

        # This epoch handling is necessary because in RPMs the epoch of the binary package is often not part of the
        # sourceRpm name, but Grype needs it to do the version comparison correctly.
        # Without this step Grype will use the wrong version string for the vulnerability match
        full_version = record.get("version")
        epoch = None

        if full_version:
            epoch_str, version, release = split_fullversion(full_version)
            if epoch_str:
                try:
                    epoch = int(epoch_str)
                except ValueError:
                    # not a valid epoch, something went wrong
                    pass

        # Get the sourceRpm info
        source_rpm = None
        if record.get("sourcepkg") not in [None, "N/A", "n/a"]:
            source_rpm = record.get("sourcepkg")

        artifact["metadataType"] = "RpmdbMetadata"
        artifact["metadata"] = {
            "sourceRpm": source_rpm,
            "epoch": epoch,
        }

        return artifact


class DpkgMapper(LinuxDistroPackageMapper):
    def __init__(self):
        super(DpkgMapper, self).__init__(engine_type="dpkg", grype_type="deb")

    def image_content_to_grype_sbom(self, record: Dict):
        """
        Adds the source package information to grype sbom

        Source package has already been through a transformations before this point - from syft sbom to analyzer manifest
        in anchore_engine/analyzers/syft/handlers/debian.py. Fortunately the value remains unchanged and does not require
        additional processing

        """

        artifact = super().image_content_to_grype_sbom(record)
        if record.get("sourcepkg") not in [None, "N/A", "n/a"]:
            artifact["metadataType"] = "DpkgMetadata"
            artifact["metadata"] = {"source": record.get("sourcepkg")}
        return artifact


class ApkgMapper(LinuxDistroPackageMapper):
    def __init__(self):
        super(ApkgMapper, self).__init__(engine_type="APKG", grype_type="apk")

    def image_content_to_grype_sbom(self, record: Dict):
        """
        Adds the origin package information to grype sbom

        Origin package has already been through a transformations before this point - from syft sbom to analyzer manifest
        in anchore_engine/analyzers/syft/handlers/alpine.py. Fortunately the value remains unchanged and does not require
        additional processing
        """

        artifact = super().image_content_to_grype_sbom(record)
        if record.get("sourcepkg") not in [None, "N/A", "n/a"]:
            artifact["metadataType"] = "ApkMetadata"
            artifact["metadata"] = {"originPackage": record.get("sourcepkg")}
        return artifact


class CPEMapper(PackageMapper):
    def fallback_cpe_generator(self, record: Dict) -> List[str]:
        return generate_fuzzy_cpes(
            record.get("package"), record.get("version"), self.engine_type
        )

    def image_content_to_grype_sbom(self, record: Dict):
        """
        Initializes grype sbom artifact and sets the location

        {
          "cpes": [
            "cpe:2.3:a:jvnet:xstream:1.3.1-hudson-8:*:*:*:*:maven:*:*",
            "cpe:2.3:a:hudson:hudson:1.3.1-hudson-8:*:*:*:*:maven:*:*",
          ],
          "implementation-version": "N/A",
          "location": "/hudson.war:WEB-INF/lib/xstream-1.3.1-hudson-8.jar",
          "maven-version": "1.3.1-hudson-8",
          "origin": "org.jvnet.hudson",
          "package": "xstream",
          "specification-version": "N/A",
          "type": "JAVA-JAR",
          "version": "1.3.1-hudson-8"
        }
        """
        artifact = super().image_content_to_grype_sbom(record)
        artifact["language"] = self.grype_language
        if record.get("location") not in [None, "N/A", "n/a"]:
            artifact["locations"] = [{"path": record.get("location")}]

        if not artifact.get("cpes"):
            artifact["cpes"] = self.fallback_cpe_generator(record)

        return artifact


class GoMapper(CPEMapper):
    def fallback_cpe_generator(self, record: Dict) -> List[str]:
        return generate_fuzzy_go_cpes(record.get("package"), record.get("version"))


class JavaMapper(CPEMapper):
    @staticmethod
    def _image_content_to_grype_metadata(metadata: Optional[Dict]) -> Dict:
        result = {}

        if not metadata or not isinstance(metadata, dict):
            return result

        pom_properties = metadata.get("pom.properties")
        if not pom_properties:
            return result

        if pom_properties:
            if isinstance(pom_properties, str):
                # copied from anchore_engine/db/entities/policy_engine.py#get_pom_properties()
                props = {}
                for line in pom_properties.splitlines():
                    # line = anchore_engine.utils.ensure_str(line)
                    if not re.match(r"\s*(#.*)?$", line):
                        kv = line.split("=")
                        key = kv[0].strip()
                        value = "=".join(kv[1:]).strip()
                        props[key] = value
                result["pomProperties"] = props
            elif isinstance(pom_properties, dict):
                result["pomProperties"] = pom_properties
            else:
                log.warn(
                    "Unknown format for pom.properties %s, skip parsing", pom_properties
                )
        return result

    def image_content_to_grype_sbom(self, record: Dict):
        artifact = super().image_content_to_grype_sbom(record)
        grype_metadata = self._image_content_to_grype_metadata(record.get("metadata"))
        if grype_metadata:
            artifact["metadataType"] = "JavaMetadata"
            artifact["metadata"] = grype_metadata
        return artifact

    def fallback_cpe_generator(self, record: Dict) -> List[str]:
        return generate_java_cpes(record)


class VulnerabilityMapper:
    _transform_id_for_feed_groups = ["vulndb"]

    @staticmethod
    def _try_parse_cvss(cvss_list: List[Dict]) -> List[CVSS]:
        """
        Best effort attempt at parsing CVSS from response. Ignores any errors raised and chugs along
        """
        cvss_objects = []
        if isinstance(cvss_list, list) and cvss_list:
            for cvss_dict in cvss_list:
                try:
                    cvss_objects.append(
                        CVSS(
                            version=cvss_dict.get("version"),
                            vector=cvss_dict.get("vector"),
                            base_score=cvss_dict.get("metrics", {}).get(
                                "baseScore", -1.0
                            ),
                            exploitability_score=cvss_dict.get("metrics", {}).get(
                                "exploitabilityScore", -1.0
                            ),
                            impact_score=cvss_dict.get("metrics", {}).get(
                                "impactScore", -1.0
                            ),
                        )
                    )
                except (AttributeError, ValueError):
                    log.debug("Ignoring error parsing CVSS dict %s", cvss_dict)

        return cvss_objects

    @staticmethod
    def _try_parse_related_vulnerabilities(
        vulns: List[Dict],
    ) -> List[NVDReference]:
        """
        Best effort attempt at parsing other vulnerabilities from grype response. Ignores any errors raised and chugs along
        """
        nvd_objects = []
        if isinstance(vulns, list) and vulns:
            for vuln_dict in vulns:
                try:
                    nvd_objects.append(
                        NVDReference(
                            vulnerability_id=vuln_dict.get("id"),
                            # description=vuln_dict.get("description"),
                            description=None,
                            severity=vuln_dict.get("severity"),
                            link=vuln_dict.get("dataSource"),
                            cvss=VulnerabilityMapper._try_parse_cvss(
                                vuln_dict.get("cvss", [])
                            ),
                        )
                    )
                except (AttributeError, ValueError):
                    log.debug(
                        "Ignoring error parsing related vulnerability dict %s",
                        vuln_dict,
                    )

        return nvd_objects

    @staticmethod
    def _try_parse_advisories(
        advisories: List[Dict],
    ) -> List[Advisory]:
        """
        Best effort attempt at parsing advisories from grype response. Ignores any errors raised and chugs along
        """
        advisory_objects = []
        if isinstance(advisories, list) and advisories:
            for advisory_dict in advisories:
                try:
                    # TODO format check with toolbox
                    advisory_objects.append(
                        Advisory(
                            id=advisory_dict.get("id"), link=advisory_dict.get("link")
                        )
                    )
                except (AttributeError, ValueError):
                    log.debug(
                        "Ignoring error parsing advisory dict %s",
                        advisory_dict,
                    )

        return advisory_objects

    @staticmethod
    def _try_parse_matched_cpes(match_dict: Dict) -> List[str]:
        """
        Best effort attempt at parsing cpes that were matched from matchDetails of grype response.

        Input is a dictionary representing a single grype match output, the attribute of interest here is matchDetails
        {
          "matchDetails": [
            {
              "matcher": "java-matcher",
              "searchedBy": {
                "namespace": "nvd",
                "cpes": [
                  "cpe:2.3:a:*:spring_framework:5.2.6.RELEASE:*:*:*:*:*:*:*"
                ]
              },
              "matchedOn": {
                "versionConstraint": "<= 4.2.9 || >= 4.3.0, <= 4.3.28 || >= 5.0.0, <= 5.0.18 || >= 5.1.0, <= 5.1.17 || >= 5.2.0, <= 5.2.8 (unknown)",
                "cpes": [
                  "cpe:2.3:a:pivotal_software:spring_framework:*:*:*:*:*:*:*:*"
                ]
              }
            }
          ]
          ...
        }
        """
        cpes = []
        if match_dict and isinstance(match_dict, dict):
            try:
                matchers = match_dict.get("matchDetails", [])
                for matcher in matchers:
                    matcher_cpes = matcher.get("searchedBy", {}).get("cpes", [])
                    if matcher_cpes:
                        cpes.extend(matcher_cpes)
            except (AttributeError, ValueError):
                log.warn("Ignoring error parsing cpes")

        return list(set(cpes))

    @staticmethod
    def _try_get_normalized_vulnerability_id(
        vulnerability_id: str,
        feed_group: str,
        nvd_references: Optional[List[NVDReference]],
    ) -> str:
        """
        This is a helper function for transforming third party vulnerability identifier to more widely used CVE identifier when possible

        For a given feed group, the function first checks if the vulnerability ID needs to be transformed.
        If so, it tries to look for a single NVD reference and returns the CVE ID of the record.
        If the vulnerability has multiple or no NVD references, then the vulnerability ID is returned as is
        """

        try:
            if (
                feed_group
                and feed_group.split(":")[0].lower()
                in VulnerabilityMapper._transform_id_for_feed_groups
            ):
                if isinstance(nvd_references, list) and len(nvd_references) == 1:
                    return nvd_references[0].vulnerability_id
                else:  # no or >1 NVD references found, can't use to a single CVE
                    return vulnerability_id
            else:
                return vulnerability_id
        except (AttributeError, IndexError, ValueError):
            return vulnerability_id

    @staticmethod
    def _try_make_link(
        vulnerability_id: Optional[str], source_url: Optional[str]
    ) -> str:
        if source_url:
            return source_url
        elif vulnerability_id:
            return "{}/query/vulnerabilities?id={}".format(
                get_api_endpoint(), vulnerability_id
            )
        else:
            return "N/A"

    def grype_to_engine_image_vulnerability(
        self,
        result: Dict,
        package_mapper: PackageMapper,
        now: datetime.datetime,
    ):
        artifact_dict = result.get("artifact")
        vuln_dict = result.get("vulnerability")

        # parse cvss fields
        cvss_objs = VulnerabilityMapper._try_parse_cvss(vuln_dict.get("cvss", []))

        # parse fix details
        fix_dict = vuln_dict.get("fix")
        fix_obj = FixedArtifact(
            versions=[], will_not_fix=False, observed_at=None, advisories=[]
        )
        if fix_dict:
            fix_obj.versions = fix_dict.get("versions", [])
            fix_obj.will_not_fix = (
                fix_dict.get("state").lower() == "wont-fix"
            )  # TODO format check with toolbox
            fix_obj.observed_at = now if fix_obj.versions else None
            fix_obj.advisories = VulnerabilityMapper._try_parse_advisories(
                fix_dict.get("advisories", [])
            )

        # parse nvd references
        nvd_objs = VulnerabilityMapper._try_parse_related_vulnerabilities(
            result.get("relatedVulnerabilities", [])
        )

        # parse package path
        pkg_path = (
            artifact_dict.get("locations")[0].get("path")
            if artifact_dict.get("locations")
            else "NA"
        )

        vuln_match = VulnerabilityMatch(
            vulnerability=Vulnerability(
                vulnerability_id=self._try_get_normalized_vulnerability_id(
                    vuln_dict.get("id"), vuln_dict.get("namespace"), nvd_objs
                ),
                description=vuln_dict.get("description"),
                severity=vuln_dict.get("severity"),
                link=self._try_make_link(
                    vuln_dict.get("id"), vuln_dict.get("dataSource")
                ),
                feed="vulnerabilities",
                feed_group=vuln_dict.get("namespace"),
                cvss=cvss_objs,
            ),
            artifact=Artifact(
                name=artifact_dict.get("name"),
                version=artifact_dict.get("version"),
                pkg_type=package_mapper.engine_type,
                location=pkg_path,
                cpe=None,  # vestige of the old system
                cpes=self._try_parse_matched_cpes(result),
            ),
            fix=fix_obj,
            match=Match(detected_at=now),
            nvd=nvd_objs,
        )

        return vuln_match


# key is the engine distro
ENGINE_DISTRO_MAPPERS = {
    "rhel": DistroMapper(
        engine_distro="rhel", grype_os="redhat", grype_like_os="fedora"
    ),
    "debian": DistroMapper(
        engine_distro="debian", grype_os="debian", grype_like_os="debian"
    ),
    "ubuntu": DistroMapper(
        engine_distro="ubuntu", grype_os="ubuntu", grype_like_os="debian"
    ),
    "alpine": DistroMapper(
        engine_distro="alpine", grype_os="alpine", grype_like_os="alpine"
    ),
    "ol": DistroMapper(
        engine_distro="ol", grype_os="oraclelinux", grype_like_os="fedora"
    ),
    "amzn": DistroMapper(
        engine_distro="amzn", grype_os="amazonlinux", grype_like_os="fedora"
    ),
    "centos": DistroMapper(
        engine_distro="centos", grype_os="centos", grype_like_os="fedora"
    ),
    "busybox": DistroMapper(
        engine_distro="busybox", grype_os="busybox", grype_like_os=""
    ),
    "sles": DistroMapper(engine_distro="sles", grype_os="sles", grype_like_os="sles"),
    "windows": DistroMapper(
        engine_distro="windows", grype_os="windows", grype_like_os=""
    ),
    "rocky": DistroMapper(
        engine_distro="rocky", grype_os="rockylinux", grype_like_os="fedora"
    ),
}


# key is the engine package type
ENGINE_PACKAGE_MAPPERS = {
    "rpm": RpmMapper(),
    "dpkg": DpkgMapper(),
    "APKG": ApkgMapper(),
    "apkg": ApkgMapper(),
    "python": CPEMapper(
        engine_type="python", grype_type="python", grype_language="python"
    ),
    "npm": CPEMapper(engine_type="npm", grype_type="npm", grype_language="javascript"),
    "gem": CPEMapper(engine_type="gem", grype_type="gem", grype_language="ruby"),
    "java": JavaMapper(
        engine_type="java", grype_type="java-archive", grype_language="java"
    ),
    "go": GoMapper(engine_type="go", grype_type="go-module", grype_language="go"),
    "binary": CPEMapper(engine_type="binary", grype_type="binary"),
    "maven": CPEMapper(
        engine_type="maven", grype_type="java-archive", grype_language="java"
    ),
    "js": CPEMapper(engine_type="js", grype_type="js", grype_language="javascript"),
    "composer": CPEMapper(engine_type="composer", grype_type="composer"),
    "nuget": CPEMapper(engine_type="nuget", grype_type="nuget"),
    "kb": KBMapper(),
}

# key is the grype package type
GRYPE_PACKAGE_MAPPERS = {
    "rpm": RpmMapper(),
    "deb": DpkgMapper(),
    "apk": ApkgMapper(),
    "python": CPEMapper(
        engine_type="python", grype_type="python", grype_language="python"
    ),
    "npm": CPEMapper(engine_type="npm", grype_type="npm", grype_language="javascript"),
    "gem": CPEMapper(engine_type="gem", grype_type="gem", grype_language="ruby"),
    "java-archive": JavaMapper(
        engine_type="java", grype_type="java-archive", grype_language="java"
    ),
    "jenkins-plugin": JavaMapper(
        engine_type="java", grype_type="jenkins-plugin", grype_language="java"
    ),
    "go-module": GoMapper(
        engine_type="go", grype_type="go-module", grype_language="go"
    ),
    "binary": CPEMapper(engine_type="binary", grype_type="binary"),
    "js": CPEMapper(engine_type="js", grype_type="js", grype_language="javascript"),
    "composer": CPEMapper(engine_type="composer", grype_type="composer"),
    "nuget": CPEMapper(engine_type="nuget", grype_type="nuget"),
    "msrc-kb": KBMapper(),
}

GRYPE_MATCH_MAPPER = VulnerabilityMapper()


def image_content_to_grype_sbom(image: Image, image_content_map: Dict) -> Dict:

    # select the distro
    distro_mapper = ENGINE_DISTRO_MAPPERS.get(image.distro_name)
    if not distro_mapper:
        log.error(
            "No distro mapper found for %s. Cannot generate sbom", image.distro_name
        )
        raise ValueError(
            f"No distro mapper found for {image.distro_name}. Cannot generate sbom"
        )

    # initialize the sbom
    sbom = dict()

    # set the schema version, this whole fn could be refactored as a versioned transformer
    # not refactoring since use of source sbom will make this transformation moot
    sbom["schema"] = {
        "version": "1.1.0",
        "url": "https://raw.githubusercontent.com/anchore/syft/main/schema/json/schema-1.1.0.json",
    }

    # set the distro information
    sbom["distro"] = distro_mapper.to_grype_distro(image.distro_version)
    sbom["source"] = {
        "type": "image",
        "target": {
            "scope": "Squashed",
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        },
    }

    artifacts = []
    sbom["artifacts"] = artifacts

    for content_type, packages in image_content_map.items():
        for package in packages:
            if content_type in nonos_package_types:
                pkg_mapper = ENGINE_PACKAGE_MAPPERS.get(content_type)
            elif content_type in ["os"] and package.get("type"):
                pkg_mapper = ENGINE_PACKAGE_MAPPERS.get(package.get("type").lower())
            else:
                pkg_mapper = None

            if not pkg_mapper:
                log.warn(
                    "No mapper found for engine image content %s, using a default mapper",
                    package,
                )
                pkg_mapper = CPEMapper(
                    engine_type=package.get("type"), grype_type=package.get("type")
                )

            try:
                artifacts.append(pkg_mapper.image_content_to_grype_sbom(package))
            except Exception:
                log.exception(
                    "Skipping sbom entry due to error in engine->grype transformation for engine image content %s",
                    package,
                )

    return sbom


def grype_to_engine_image_vulnerabilities(grype_response):
    """
    Transform grype results into engine vulnerabilities
    """

    now = datetime.datetime.utcnow()
    matches = grype_response.get("matches", []) if grype_response else []
    results = []

    for item in matches:
        artifact = item.get("artifact")

        pkg_mapper = GRYPE_PACKAGE_MAPPERS.get(artifact.get("type"))
        if not pkg_mapper:
            log.warn(
                "No mapper found for grype artifact type %s, skipping vulnerability match",
                artifact.get("type"),
            )
            continue

        try:
            results.append(
                GRYPE_MATCH_MAPPER.grype_to_engine_image_vulnerability(
                    item, pkg_mapper, now
                )
            )
        except Exception:
            log.exception(
                "Ignoring error in grype->engine transformation for vulnerability match, skipping it from report",
            )

    return results


class EngineGrypeDBMapper:
    return_el_template = {
        "id": None,
        "namespace": None,
        "severity": None,
        "link": None,
        "affected_packages": [],
        "description": None,
        "references": [],
        "nvd_data": [],
        "vendor_data": [],
    }
    cvss_template = {
        "version": None,
        "vector_string": None,
        "severity": None,
        "base_metrics": {
            "base_score": -1.0,
            "expolitability_score": -1.0,
            "impact_score": -1.0,
        },
    }

    def _transform_cvss_score(self, cvss):
        score_dict = copy.deepcopy(self.cvss_template)
        version = cvss.get("Version")
        base_score = cvss.get("Metrics", {}).get("BaseScore", -1.0)

        score_dict["version"] = version
        score_dict["vector_string"] = cvss.get("Vector", None)
        score_dict["base_metrics"] = dict(
            base_score=base_score,
            expolitability_score=cvss.get("Metrics", {}).get(
                "ExploitabilityScore", -1.0
            ),
            impact_score=cvss.get("Metrics", {}).get("ImpactScore", -1.0),
        )

        # NVD qualitative ratings based on base score https://nvd.nist.gov/vuln-metrics/cvss
        severity = "Unknown"
        if base_score is not None:
            if version and version.startswith("2"):
                if base_score <= 3.9:
                    severity = "Low"
                elif base_score <= 6.9:
                    severity = "Medium"
                elif base_score <= 10:
                    severity = "High"
            elif version and version.startswith("3"):
                if base_score <= 3.9:
                    severity = "Low"
                elif base_score <= 6.9:
                    severity = "Medium"
                elif base_score <= 8.9:
                    severity = "High"
                elif base_score <= 10:
                    severity = "Critical"
        score_dict["severity"] = severity

        return score_dict

    def _transform_cvss(self, vulnerability_id: str, cvss: Dict) -> Dict:
        version = cvss["Version"]
        if version.startswith("2"):
            score_dict = self._transform_cvss_score(cvss)
            result = {
                "cvss_v2": score_dict,
                "cvss_v3": None,  # legacy mode compatibility
                "id": vulnerability_id,
            }
        elif version.startswith("3"):
            score_dict = self._transform_cvss_score(cvss)
            result = {
                "cvss_v2": None,  # legacy mode compatibility
                "cvss_v3": score_dict,
                "id": vulnerability_id,
            }
        else:
            log.warn(
                "Omitting the following cvss with unknown version from vulnerability %s: %s",
                vulnerability_id,
                cvss,
            )
            result = None

        return result

    def _cvss_from_grype_raw_result(
        self, grype_vulnerability_metadata: GrypeVulnerabilityMetadata
    ) -> List:
        """
        Given the raw grype vulnerability input, returns a dict of its cvss scores
        """
        # Transform the cvss blocks
        cvss_combined = grype_vulnerability_metadata.deserialized_cvss
        vulnerability_cvss_data = []

        for cvss in cvss_combined:
            result = self._transform_cvss(grype_vulnerability_metadata.id, cvss)
            if result:
                vulnerability_cvss_data.append(result)

        return vulnerability_cvss_data

    @staticmethod
    def _transform_urls(urls: List[str]) -> List:
        results = []
        if not urls or not isinstance(urls, list):
            return results

        results = [{"source": "N/A", "url": item} for item in urls]

        return results

    def to_engine_vulnerabilities(
        self,
        grype_vulnerabilities: List,
        related_nvd_metadata_records: List[GrypeVulnerabilityMetadata],
    ):
        """
        Receives query results from grype_db and returns a list of vulnerabilities mapped
        into the data structure engine expects.
        """
        intermediate_tuple_list = {}

        # construct the dictionary that maps cve-id->cvss-score
        nvd_cvss_data_map = {
            item.id: self._cvss_from_grype_raw_result(item)
            for item in related_nvd_metadata_records
        }

        for grype_raw_result in grype_vulnerabilities:
            grype_vulnerability = grype_raw_result.GrypeVulnerability
            grype_vulnerability_metadata = grype_raw_result.GrypeVulnerabilityMetadata

            vuln_dict = intermediate_tuple_list.get(
                (
                    grype_vulnerability_metadata.id,
                    grype_vulnerability_metadata.namespace,
                )
            )

            if not vuln_dict:
                vuln_dict = copy.deepcopy(self.return_el_template)
                intermediate_tuple_list[
                    (
                        grype_vulnerability_metadata.id,
                        grype_vulnerability_metadata.namespace,
                    )
                ] = vuln_dict

                vuln_dict["id"] = grype_vulnerability_metadata.id
                vuln_dict["namespace"] = grype_vulnerability_metadata.namespace
                vuln_dict["description"] = grype_vulnerability_metadata.description
                vuln_dict["severity"] = grype_vulnerability_metadata.severity
                vuln_dict["link"] = VulnerabilityMapper._try_make_link(
                    grype_vulnerability_metadata.id,
                    grype_vulnerability_metadata.data_source,
                )
                vuln_dict["references"] = self._transform_urls(
                    grype_vulnerability_metadata.deserialized_urls
                )

                # populate nvd and vendor cvss data depending on the namespace
                if "nvd" in grype_vulnerability_metadata.namespace.lower():
                    vuln_dict["nvd_data"] = nvd_cvss_data_map.get(
                        grype_vulnerability_metadata.id
                    )
                    vuln_dict["vendor_data"] = []
                else:
                    vuln_dict["vendor_data"] = self._cvss_from_grype_raw_result(
                        grype_vulnerability_metadata
                    )

                    # populate nvd data of related vulnerabilities
                    vuln_dict["nvd_data"] = []
                    related_vulns = (
                        grype_vulnerability.deserialized_related_vulnerabilities
                    )
                    if related_vulns:
                        for related_vuln in related_vulns:
                            # check related vuln is in nvd namespace, bail otherwise
                            if "nvd" not in related_vuln["Namespace"].lower():
                                continue

                            # retrieve cvss score from pre-populated map
                            nvd_cvss = nvd_cvss_data_map.get(related_vuln["ID"])
                            if nvd_cvss:
                                vuln_dict["nvd_data"].extend(nvd_cvss)

            # results are produced by left outer join, hence the check
            if grype_vulnerability:

                # Transform the versions block
                if grype_vulnerability.version_constraint:
                    version_strings = [
                        version.strip(" '\"")
                        for version in grype_vulnerability.version_constraint.split(
                            "||"
                        )
                    ]
                    version = ",".join(version_strings)
                else:
                    version = "*"

                # Populate affected_packages
                vuln_dict["affected_packages"].append(
                    {
                        "name": grype_vulnerability.package_name,
                        "type": grype_vulnerability.version_format,
                        "version": version,
                        "will_not_fix": grype_vulnerability.fix_state == "wont-fix",
                    }
                )

        for _, vulnerability in intermediate_tuple_list.items():
            unique_affected_packages = set(
                frozenset(affected_package.items())
                for affected_package in vulnerability["affected_packages"]
            )
            vulnerability["affected_packages"] = [
                dict(affected_package) for affected_package in unique_affected_packages
            ]

        return list(intermediate_tuple_list.values())
