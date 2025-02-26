"""
### Summary

Ensures that the central grafana at https://grafana.pilot.2i2c.cloud is configured to use as datasource the authenticated prometheus instances of all the clusters that we run.

### How to use

This is meant to by run as a script from the command line, like so:

$ python deployer/grafana_datasources_manager.py

"""

import argparse
import json

import requests
from file_acquisition import find_absolute_path_to_cluster_file, get_decrypted_file
from helm_upgrade_decision import get_all_cluster_yaml_files
from ruamel.yaml import YAML
from utils import print_colour

yaml = YAML(typ="safe")


def build_datasource_details(cluster_name):
    """Builds the payload needed to create an authenticated datasource in Grafana for `cluster_name`.

    Args:
        cluster_name: name of the cluster
    Returns:
        dict object: req payload to be consumed by Grafana
    """
    # Get the prometheus address for cluster_name
    datasource_url = get_cluster_prometheus_address(cluster_name)

    # Get the credentials of this prometheus instance
    prometheus_creds = get_cluster_prometheus_creds(cluster_name)

    datasource_details = {
        "name": cluster_name,
        "type": "prometheus",
        "access": "proxy",
        "url": f"https://{datasource_url}",
        "basicAuth": True,
        "basicAuthUser": prometheus_creds["username"],
        "secureJsonData": {"basicAuthPassword": prometheus_creds["password"]},
    }

    return datasource_details


def get_central_grafana_url(central_cluster_name):
    cluster_config_dir_path = find_absolute_path_to_cluster_file(
        central_cluster_name
    ).parent

    config_file = cluster_config_dir_path.joinpath("support.values.yaml")
    with open(config_file) as f:
        support_config = yaml.load(f)

    grafana_tls_config = (
        support_config.get("grafana", {}).get("ingress", {}).get("tls", [])
    )

    if not grafana_tls_config:
        raise ValueError(
            f"No tls config was found for the Grafana instance of {central_cluster_name}. Please consider enable it before using it as the central Grafana."
        )

    # We only have one tls host right now. Modify this when things change.
    return grafana_tls_config[0]["hosts"][0]


def get_cluster_prometheus_address(cluster_name):
    """Retrieves the address of the prometheus instance running on the `cluster_name` cluster.
    This address is stored in the `support.values.yaml` file of each cluster config directory.

    Args:
        cluster_name: name of the cluster
    Returns:
        string object: https address of the prometheus instance
    Raises ValueError if
        - `prometheusIngressAuthSecret` isn't configured
        - `support["prometheus"]["server"]["ingress"]["tls"]` doesn't exist
    """
    cluster_config_dir_path = find_absolute_path_to_cluster_file(cluster_name).parent

    config_file = cluster_config_dir_path.joinpath("support.values.yaml")
    with open(config_file) as f:
        support_config = yaml.load(f)

    # Don't return the address if the prometheus instance wasn't securely exposed to the outside.
    if not support_config.get("prometheusIngressAuthSecret", {}).get("enabled", False):
        raise ValueError(
            f"`prometheusIngressAuthSecret` wasn't configured for {cluster_name}"
        )

    tls_config = (
        support_config.get("prometheus", {})
        .get("server", {})
        .get("ingress", {})
        .get("tls", [])
    )

    if not tls_config:
        raise ValueError(
            f"No tls config was found for the prometheus instance of {cluster_name}"
        )

    # We only have one tls host right now. Modify this when things change.
    return tls_config[0]["hosts"][0]


def get_cluster_prometheus_creds(cluster_name):
    """Retrieves the credentials of the prometheus instance running on the `cluster_name` cluster.
    These credentials are stored in `enc-support.secret.values.yaml` file of each cluster config directory.

    Args:
        cluster_name: name of the cluster
    Returns:
        dict object: {username: `username`, password: `password`}
    """
    cluster_config_dir_path = find_absolute_path_to_cluster_file(cluster_name).parent

    config_filename = cluster_config_dir_path.joinpath("enc-support.secret.values.yaml")

    with get_decrypted_file(config_filename) as decrypted_path:
        with open(decrypted_path) as f:
            prometheus_config = yaml.load(f)

    return prometheus_config.get("prometheusIngressAuthSecret", {})


def get_central_grafana_token(cluster_name):
    """Returns the access token of the Grafana located in `cluster_name` cluster.
    This access token should have enough permissions to create datasources.
    """
    # Get the location of the file that stores the central grafana token
    cluster_config_dir_path = find_absolute_path_to_cluster_file(cluster_name).parent

    grafana_token_file = (cluster_config_dir_path).joinpath(
        "enc-grafana-token.secret.yaml"
    )

    # Read the secret grafana token file
    with get_decrypted_file(grafana_token_file) as decrypted_file_path:
        with open(decrypted_file_path) as f:
            config = yaml.load(f)

    return config["grafana_token"]


def build_request_headers(cluster_name):
    token = get_central_grafana_token(cluster_name)

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    return headers


def get_clusters_used_as_datasources(cluster_name, datasource_endpoint):
    """Returns a list of cluster names that have prometheus instances already defined as datasources of the centralized Grafana."""
    headers = build_request_headers(cluster_name)
    # Get a list of all the currently existing datasources
    response = requests.get(datasource_endpoint, headers=headers)

    if response.status_code != 200:
        print(
            f"An error occured when retrieving the datasources from {datasource_endpoint}. \n Error was {response.text}."
        )
        response.raise_for_status()

    datasources = response.json()
    return [datasource["name"] for datasource in datasources]


def main():
    argparser = argparse.ArgumentParser(
        description="""A command line tool to update Grafana
        datasources.
        """
    )

    argparser.add_argument(
        "cluster_name",
        type=str,
        nargs="?",
        help="The name of the cluster where the Grafana lives",
        default="2i2c",
    )

    args = argparser.parse_args()
    central_cluster = args.cluster_name
    grafana_host = get_central_grafana_url(central_cluster)
    datasource_endpoint = f"https://{grafana_host}/api/datasources"

    # Get a list of the clusters that already have their prometheus instances used as datasources
    datasources = get_clusters_used_as_datasources(central_cluster, datasource_endpoint)

    # Get a list of filepaths to all cluster.yaml files in the repo
    cluster_files = get_all_cluster_yaml_files()

    print("Searching for clusters that aren't Grafana datasources...")
    # Count how many clusters we can't add as datasources for logging
    exceptions = 0
    for cluster_file in cluster_files:
        # Read in the cluster.yaml file
        with open(cluster_file) as f:
            cluster_config = yaml.load(f)

        # Get the cluster's name
        cluster_name = cluster_config.get("name", {})
        if cluster_name and cluster_name not in datasources:
            print(f"Found {cluster_name} cluster. Checking if it can be added...")
            # Build the datasource details for the instances that aren't configures as datasources
            try:
                datasource_details = build_datasource_details(cluster_name)
                req_body = json.dumps(datasource_details)

                # Tell Grafana to create and register a datasource for this cluster
                headers = build_request_headers(central_cluster)
                response = requests.post(
                    datasource_endpoint, data=req_body, headers=headers
                )
                if response.status_code != 200:
                    print(
                        f"An error occured when creating the datasource. \nError was {response.text}."
                    )
                    response.raise_for_status()
                print_colour(
                    f"Successfully created a new datasource for {cluster_name}!"
                )
            except Exception as e:
                print_colour(
                    f"An error occured for {cluster_name}.\nError was: {e}.\nSkipping...",
                    "yellow",
                )
                exceptions += 1
                pass

    print_colour(
        f"Failed to add {exceptions} clusters as datasources. See errors above!", "red"
    )
    print_colour(
        f"Successfully retrieved {len(datasources)} existing datasources! {datasources}"
    )


if __name__ == "__main__":
    main()
