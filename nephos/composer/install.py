#   Copyright [2018] [Alejandro Vicente Grabovetsky via AID:Tech]
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at#
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from kubernetes.client.rest import ApiException

from nephos.composer.connection_template import json_ct
from nephos.fabric.crypto import admin_creds
from nephos.fabric.utils import get_helm_pod
from nephos.fabric.settings import get_namespace, get_version
from nephos.helpers.helm import (
    HelmPreserve,
    helm_check,
    helm_extra_vars,
    helm_install,
    helm_upgrade,
)
from nephos.helpers.k8s import (
    get_app_info,
    cm_create,
    cm_read,
    ingress_read,
    secret_from_file,
)


def get_composer_data(opts, verbose=False):
    """Get Composer deployment data.

    Args:
        opts (dict): Nephos options dict.
        verbose (bool): Verbosity. False by default.

    Returns:
        dict: Data related to the Composer deployment (URI & API key)
    """
    peer_namespace = get_namespace(opts, opts["peers"]["msp"])
    composer_name = opts["composer"]["name"] + "-hl-composer-rest"
    data = get_app_info(
        peer_namespace,
        composer_name,
        composer_name,
        secret_key="COMPOSER_APIKEY",
        verbose=verbose,
    )
    return data


def composer_connection(opts, verbose=False):
    """Composer connection setup.

    This creates a ConfigMap on K8S with the Hyperledger Composer connection.

    Args:
        opts (dict): Nephos options dict.
        verbose (bool): Verbosity. False by default.
    """
    peer_namespace = get_namespace(opts, opts["peers"]["msp"])
    # TODO: This could be a single function
    peer_msp = opts["peers"]["msp"]
    peer_ca = opts["msps"][peer_msp]["ca"]
    ca_namespace = opts["cas"][peer_ca]["namespace"]
    ingress_urls = ingress_read(
        peer_ca + "-hlf-ca", namespace=ca_namespace, verbose=verbose
    )
    peer_ca_url = ingress_urls[0]
    try:
        cm_read(opts["composer"]["secret_connection"], peer_namespace, verbose=verbose)
    except ApiException:
        # Set up connection.json
        # TODO: Improve json_ct to work entirely with opts structure
        cm_data = {
            "connection.json": json_ct(
                opts,
                peer_ca,
                peer_ca_url,
                "AidTech",
                None,
                peer_msp,
                opts["peers"]["channel_name"],
            )
        }
        cm_create(
            cm_data,
            opts["composer"]["secret_connection"],
            peer_namespace,
            verbose=verbose,
        )


def deploy_composer(opts, upgrade=False, verbose=False):
    """Deploy Hyperledger Composer on K8S.

    We use the hl-composer Helm chart as a basis to deploying Composer
    on K8S. Please note that Composer is unmaintained and may eventually
    be deprecated from this repository as we migrate to raw Fabric.

    Args:
        opts (dict): Nephos options dict.
        upgrade (bool): Do we upgrade the deployment? False by default.
        verbose (bool): Verbosity. False by default.
    """
    peer_namespace = get_namespace(opts, opts["peers"]["msp"])
    # Ensure BNA exists
    secret_from_file(
        secret=opts["composer"]["secret_bna"], namespace=peer_namespace, verbose=verbose
    )
    composer_connection(opts, verbose=verbose)

    # Start Composer
    version = get_version(opts, "hl-composer")
    config_yaml = "{dir}/hl-composer/{release}.yaml".format(
        dir=opts["core"]["dir_values"], release=opts["composer"]["name"]
    )
    if not upgrade:
        extra_vars = helm_extra_vars(version=version, config_yaml=config_yaml)
        helm_install(
            opts["core"]["chart_repo"],
            "hl-composer",
            opts["composer"]["name"],
            peer_namespace,
            extra_vars=extra_vars,
            verbose=verbose,
        )
    else:
        preserve = (
            HelmPreserve(
                peer_namespace,
                "{}-hl-composer-rest".format(opts["composer"]["name"]),
                "COMPOSER_APIKEY",
                "rest.config.apiKey",
            ),
        )
        extra_vars = helm_extra_vars(
            version=version, config_yaml=config_yaml, preserve=preserve
        )
        helm_upgrade(
            opts["core"]["chart_repo"],
            "hl-composer",
            opts["composer"]["name"],
            extra_vars=extra_vars,
            verbose=verbose,
        )
    helm_check("hl-composer", opts["composer"]["name"], peer_namespace, pod_num=3)


def setup_card(opts, msp_path, user_name, roles, network=None, verbose=False):
    """Setup the Card for Hyperledger Composer.

    Args:
        opts (dict): Nephos options dict.
        msp_path (str): Path to the MSP on the Composer CLI.
        user_name (str): Name of user for identity card.
        network (str): Name of network for identity card.
        roles (Iterable): Roles to assign to identity card.
        verbose (bool): Verbosity. False by default.
    """

    peer_namespace = get_namespace(opts, opts["peers"]["msp"])
    hlc_cli_ex = get_helm_pod(
        peer_namespace, opts["composer"]["name"], "hl-composer", verbose=verbose
    )

    # Set up the PeerAdmin card
    ls_res, _ = hlc_cli_ex.execute(
        "composer card list --card {admin_name}@{network}".format(
            admin_name=user_name, network=network
        )
    )

    if roles:
        roles_string = "-r " + " -r ".join(roles) + " "
    else:
        roles_string = ""

    if not ls_res:
        hlc_cli_ex.execute(
            (
                "composer card create "
                + ("-n {network} " if network else "")
                + "-p /hl_config/hlc-connection/connection.json "
                + "-u {admin_name} -c {msp_path}/signcerts/cert.pem "
                + "-k {msp_path}/keystore/key.pem "
                + "{roles_string}"
                + "--file /home/composer/{admin_name}@{network}"
            ).format(
                msp_path=msp_path,
                admin_name=user_name,
                roles_string=roles_string,
                network=network,
            )
        )
        hlc_cli_ex.execute(
            "composer card import "
            + "--file /home/composer/{admin_name}@{network}.card".format(
                admin_name=user_name, network=network
            )
        )


def setup_admin(opts, verbose=False):
    """Setup Network admin

    Args:
        opts (dict): Nephos options dict.
        verbose (bool): Verbosity. False by default.
    """
    setup_card(
        opts,
        msp_path="/hl_config/admin",
        user_name="PeerAdmin",
        roles=("PeerAdmin", "ChannelAdmin"),
        verbose=verbose,
    )


def install_network(opts, verbose=False):
    """Install Hyperledger Composer network.

    Args:
        opts (dict): Nephos options dict.
        verbose (bool): Verbosity. False by default.
    """
    peer_namespace = get_namespace(opts, opts["peers"]["msp"])
    hlc_cli_ex = get_helm_pod(
        peer_namespace, opts["composer"]["name"], "hl-composer", verbose=verbose
    )

    # Install network
    # TODO: Getting BNA could be a helper function
    bna, _ = hlc_cli_ex.execute("ls /hl_config/blockchain_network")
    bna_name, bna_rem = bna.split("_")
    bna_version, _ = bna_rem.split(".bna")
    # TODO: This could be a single function
    peer_msp = opts["peers"]["msp"]
    bna_admin = opts["msps"][peer_msp]["org_admin"]
    admin_creds(opts, peer_msp, verbose=verbose)
    bna_pw = opts["msps"][peer_msp]["org_adminpw"]

    ls_res, _ = hlc_cli_ex.execute(
        "composer card list --card {bna_admin}@{bna_name}".format(
            bna_admin=bna_admin, bna_name=bna_name
        )
    )

    if not ls_res:
        hlc_cli_ex.execute(
            (
                "composer network install --card PeerAdmin@hlfv1 "
                + "--archiveFile /hl_config/blockchain_network/{bna}"
            ).format(bna=bna)
        )
        hlc_cli_ex.execute(
            (
                "composer network start "
                + "--card PeerAdmin@hlfv1 "
                + "--networkName {bna_name} --networkVersion {bna_version} "
                + "--networkAdmin {bna_admin} --networkAdminEnrollSecret {bna_pw}"
            ).format(
                bna_name=bna_name,
                bna_version=bna_version,
                bna_admin=bna_admin,
                bna_pw=bna_pw,
            )
        )
        hlc_cli_ex.execute(
            "composer card import --file {bna_admin}@{bna_name}.card".format(
                bna_admin=bna_admin, bna_name=bna_name
            )
        )

    hlc_cli_ex.execute(
        "composer network ping --card {bna_admin}@{bna_name}".format(
            bna_admin=bna_admin, bna_name=bna_name
        )
    )
