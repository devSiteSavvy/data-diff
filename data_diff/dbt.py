import os
import time
import webbrowser
from typing import List, Optional, Dict
import keyring

import pydantic
import rich
from rich.prompt import Confirm

from . import connect_to_table, diff_tables, Algorithm
from .cloud import DatafoldAPI, TCloudApiDataDiff, TCloudApiOrgMeta, get_or_create_data_source
from .dbt_parser import DbtParser, PROJECT_FILE
from .tracking import (
    set_entrypoint_name,
    set_dbt_user_id,
    set_dbt_version,
    set_dbt_project_id,
    create_end_event_json,
    create_start_event_json,
    send_event_json,
    is_tracking_enabled,
)
from .utils import (
    dbt_diff_string_template,
    getLogger,
    columns_added_template,
    columns_removed_template,
    no_differences_template,
    columns_type_changed_template,
    run_as_daemon,
    truncate_error,
    print_version_info,
)

logger = getLogger(__name__)


class TDiffVars(pydantic.BaseModel):
    dev_path: List[str]
    prod_path: List[str]
    primary_keys: List[str]
    connection: Dict[str, Optional[str]]
    threads: Optional[int] = None
    where_filter: Optional[str] = None
    include_columns: List[str]
    exclude_columns: List[str]


def dbt_diff(
    profiles_dir_override: Optional[str] = None,
    project_dir_override: Optional[str] = None,
    is_cloud: bool = False,
    dbt_selection: Optional[str] = None,
) -> None:
    print_version_info()
    diff_threads = []
    set_entrypoint_name("CLI-dbt")
    dbt_parser = DbtParser(profiles_dir_override, project_dir_override)
    models = dbt_parser.get_models(dbt_selection)
    datadiff_variables = dbt_parser.get_datadiff_variables()
    config_prod_database = datadiff_variables.get("prod_database")
    config_prod_schema = datadiff_variables.get("prod_schema")
    config_prod_custom_schema = datadiff_variables.get("prod_custom_schema")
    datasource_id = datadiff_variables.get("datasource_id")
    set_dbt_user_id(dbt_parser.dbt_user_id)
    set_dbt_version(dbt_parser.dbt_version)
    set_dbt_project_id(dbt_parser.dbt_project_id)

    if datadiff_variables.get("custom_schemas") is not None:
        logger.warning(
            "vars: data_diff: custom_schemas: is no longer used and can be removed.\nTo utilize custom schemas, see the documentation here: https://docs.datafold.com/development_testing/open_source"
        )

    if is_cloud:
        api = _initialize_api()
        # exit so the user can set the key
        if not api:
            return
        org_meta = api.get_org_meta()

        if datasource_id is None:
            rich.print("[red]Data source ID not found in dbt_project.yml")
            is_create_data_source = Confirm.ask("Would you like to create a new data source?")
            if is_create_data_source:
                datasource_id = get_or_create_data_source(api=api, dbt_parser=dbt_parser)
                rich.print(f'To use the data source in next runs, please, update your "{PROJECT_FILE}" with a block:')
                rich.print(f"[green]vars:\n  data_diff:\n    datasource_id: {datasource_id}\n")
                rich.print(
                    "Read more about Datafold vars in docs: "
                    "https://docs.datafold.com/os_diff/dbt_integration/#configure-a-data-source\n"
                )
            else:
                raise ValueError(
                    "Datasource ID not found, include it as a dbt variable in the dbt_project.yml. "
                    "\nvars:\n data_diff:\n   datasource_id: 1234"
                )

        data_source = api.get_data_source(datasource_id)
        dbt_parser.set_casing_policy_for(connection_type=data_source.type)
        rich.print("[green][bold]\nDiffs in progress...[/][/]\n")

    else:
        dbt_parser.set_connection()

    for model in models:
        diff_vars = _get_diff_vars(
            dbt_parser, config_prod_database, config_prod_schema, config_prod_custom_schema, model
        )

        if diff_vars.primary_keys:
            if is_cloud:
                diff_thread = run_as_daemon(_cloud_diff, diff_vars, datasource_id, api, org_meta)
                diff_threads.append(diff_thread)
            else:
                _local_diff(diff_vars)
        else:
            rich.print(
                _diff_output_base(".".join(diff_vars.dev_path), ".".join(diff_vars.prod_path))
                + "Skipped due to unknown primary key. Add uniqueness tests, meta, or tags.\n"
            )

    # wait for all threads
    if diff_threads:
        for thread in diff_threads:
            thread.join()


def _get_diff_vars(
    dbt_parser: "DbtParser",
    config_prod_database: Optional[str],
    config_prod_schema: Optional[str],
    config_prod_custom_schema: Optional[str],
    model,
) -> TDiffVars:
    dev_database = model.database
    dev_schema = model.schema_

    primary_keys = dbt_parser.get_pk_from_model(model, dbt_parser.unique_columns, "primary-key")

    # "custom" dbt config database
    if model.config.database:
        prod_database = model.config.database
    elif config_prod_database:
        prod_database = config_prod_database
    else:
        prod_database = dev_database

    # prod schema name differs from dev schema name
    if config_prod_schema:
        custom_schema = model.config.schema_

        # the model has a custom schema config(schema='some_schema')
        if custom_schema:
            if not config_prod_custom_schema:
                raise ValueError(
                    f"Found a custom schema on model {model.name}, but no value for\nvars:\n  data_diff:\n    prod_custom_schema:\nPlease set a value!\n"
                    + "For more details see: https://docs.datafold.com/development_testing/open_source"
                )
            prod_schema = config_prod_custom_schema.replace("<custom_schema>", custom_schema)
        # no custom schema, use the default
        else:
            prod_schema = config_prod_schema
    else:
        prod_schema = dev_schema

    if dbt_parser.requires_upper:
        dev_qualified_list = [x.upper() for x in [dev_database, dev_schema, model.alias] if x]
        prod_qualified_list = [x.upper() for x in [prod_database, prod_schema, model.alias] if x]
        primary_keys = [x.upper() for x in primary_keys]
    else:
        dev_qualified_list = [x for x in [dev_database, dev_schema, model.alias] if x]
        prod_qualified_list = [x for x in [prod_database, prod_schema, model.alias] if x]

    datadiff_model_config = dbt_parser.get_datadiff_model_config(model.meta)

    return TDiffVars(
        dev_path=dev_qualified_list,
        prod_path=prod_qualified_list,
        primary_keys=primary_keys,
        connection=dbt_parser.connection,
        threads=dbt_parser.threads,
        where_filter=datadiff_model_config.where_filter,
        include_columns=datadiff_model_config.include_columns,
        exclude_columns=datadiff_model_config.exclude_columns,
    )


def _local_diff(diff_vars: TDiffVars) -> None:
    dev_qualified_str = ".".join(diff_vars.dev_path)
    prod_qualified_str = ".".join(diff_vars.prod_path)
    diff_output_str = _diff_output_base(dev_qualified_str, prod_qualified_str)

    table1 = connect_to_table(diff_vars.connection, dev_qualified_str, tuple(diff_vars.primary_keys), diff_vars.threads)
    table2 = connect_to_table(
        diff_vars.connection, prod_qualified_str, tuple(diff_vars.primary_keys), diff_vars.threads
    )

    table1_columns = table1.get_schema()
    try:
        table2_columns = table2.get_schema()
    # Not ideal, but we don't have more specific exceptions yet
    except Exception as ex:
        logger.debug(ex)
        diff_output_str += "[red]New model or no access to prod table.[/] \n"
        rich.print(diff_output_str)
        return

    table1_column_names = set(table1_columns.keys())
    table2_column_names = set(table2_columns.keys())
    column_set = table1_column_names.intersection(table2_column_names)
    columns_added = table1_column_names.difference(table2_column_names)
    columns_removed = table2_column_names.difference(table1_column_names)
    # col type is i = 1 in tuple
    columns_type_changed = {
        k for k, v in table1_columns.items() if k in table2_columns and v[1] != table2_columns[k][1]
    }

    if columns_added:
        diff_output_str += columns_added_template(columns_added)

    if columns_removed:
        diff_output_str += columns_removed_template(columns_removed)

    if columns_type_changed:
        diff_output_str += columns_type_changed_template(columns_type_changed)
        column_set = column_set.difference(columns_type_changed)

    column_set = column_set - set(diff_vars.primary_keys)

    if diff_vars.include_columns:
        column_set = {x for x in column_set if x.upper() in [y.upper() for y in diff_vars.include_columns]}

    if diff_vars.exclude_columns:
        column_set = {x for x in column_set if x.upper() not in [y.upper() for y in diff_vars.exclude_columns]}

    extra_columns = tuple(column_set)

    diff = diff_tables(
        table1,
        table2,
        threaded=True,
        algorithm=Algorithm.JOINDIFF,
        extra_columns=extra_columns,
        where=diff_vars.where_filter,
        skip_null_keys=True,
    )

    if list(diff):
        diff_output_str += f"{diff.get_stats_string(is_dbt=True)} \n"
        rich.print(diff_output_str)
    else:
        diff_output_str += no_differences_template()
        rich.print(diff_output_str)


def _initialize_api() -> Optional[DatafoldAPI]:
    datafold_host = os.environ.get("DATAFOLD_HOST")
    if datafold_host is None:
        datafold_host = "https://app.datafold.com"
    datafold_host = datafold_host.rstrip("/")
    rich.print(f"Cloud datafold host: {datafold_host}")

    api_key = os.environ.get("DATAFOLD_API_KEY")
    if not api_key:
        rich.print("[red]API key not found. Getting from the keyring service")
        api_key = keyring.get_password("data-diff", "DATAFOLD_API_KEY")
        if not api_key:
            rich.print("[red]API key not found, add it as an environment variable called DATAFOLD_API_KEY.")

            yes_or_no = Confirm.ask("Would you like to generate a new API key?")
            if yes_or_no:
                webbrowser.open(f"{datafold_host}/login?next={datafold_host}/users/me")
                rich.print('After generating, please, perform in the terminal "export DATAFOLD_API_KEY=<key>"')
                return None
            else:
                raise ValueError("Cannot initialize API because the API key is not provided")

    rich.print("Saving the API key to the system keyring service")
    try:
        keyring.set_password("data-diff", "DATAFOLD_API_KEY", api_key)
    except Exception as e:
        rich.print(f"[red]Failed when saving the API key to the system keyring service. Reason: {e}")

    return DatafoldAPI(api_key=api_key, host=datafold_host)


def _cloud_diff(diff_vars: TDiffVars, datasource_id: int, api: DatafoldAPI, org_meta: TCloudApiOrgMeta) -> None:
    diff_output_str = _diff_output_base(".".join(diff_vars.dev_path), ".".join(diff_vars.prod_path))
    payload = TCloudApiDataDiff(
        data_source1_id=datasource_id,
        data_source2_id=datasource_id,
        table1=diff_vars.prod_path,
        table2=diff_vars.dev_path,
        pk_columns=diff_vars.primary_keys,
        filter1=diff_vars.where_filter,
        filter2=diff_vars.where_filter,
        include_columns=diff_vars.include_columns,
        exclude_columns=diff_vars.exclude_columns,
    )

    if is_tracking_enabled():
        event_json = create_start_event_json({"is_cloud": True, "datasource_id": datasource_id})
        run_as_daemon(send_event_json, event_json)

    start = time.monotonic()
    error = None
    diff_id = None
    diff_url = None
    try:
        diff_id = api.create_data_diff(payload=payload)
        diff_url = f"{api.host}/datadiffs/{diff_id}/overview"
        rich.print(f"{diff_vars.dev_path[-1]}: {diff_url}")

        if diff_id is None:
            raise Exception(f"Api response did not contain a diff_id")

        diff_results = api.poll_data_diff_results(diff_id)

        rows_added_count = diff_results.pks.exclusives[1]
        rows_removed_count = diff_results.pks.exclusives[0]

        rows_updated = diff_results.values.rows_with_differences
        total_rows = diff_results.values.total_rows
        rows_unchanged = int(total_rows) - int(rows_updated)
        diff_percent_list = {
            x.column_name: str(x.match) + "%" for x in diff_results.values.columns_diff_stats if x.match != 100.0
        }
        columns_added = diff_results.schema_.exclusive_columns[1]
        columns_removed = diff_results.schema_.exclusive_columns[0]
        column_type_changes = diff_results.schema_.column_type_differs

        if columns_added:
            diff_output_str += columns_added_template(columns_added)

        if columns_removed:
            diff_output_str += columns_removed_template(columns_removed)

        if column_type_changes:
            diff_output_str += columns_type_changed_template(column_type_changes)

        if any([rows_added_count, rows_removed_count, rows_updated]):
            diff_output = dbt_diff_string_template(
                rows_added_count,
                rows_removed_count,
                rows_updated,
                str(rows_unchanged),
                diff_percent_list,
                "Value Match Percent:",
            )
            diff_output_str += f"\n{diff_url}\n {diff_output} \n"
            rich.print(diff_output_str)
        else:
            diff_output_str += f"\n{diff_url}\n{no_differences_template()}\n"
            rich.print(diff_output_str)

    except BaseException as ex:  # Catch KeyboardInterrupt too
        error = ex
    finally:
        # we don't currently have much of this information
        # but I imagine a future iteration of this _cloud method
        # will poll for results
        if is_tracking_enabled():
            err_message = truncate_error(repr(error))
            event_json = create_end_event_json(
                is_success=error is None,
                runtime_seconds=time.monotonic() - start,
                data_source_1_type="",
                data_source_2_type="",
                table1_count=0,
                table2_count=0,
                diff_count=0,
                error=err_message,
                diff_id=diff_id,
                is_cloud=True,
                org_id=org_meta.org_id,
                org_name=org_meta.org_name,
                user_id=org_meta.user_id,
            )
            send_event_json(event_json)

        if error:
            rich.print(diff_output_str)
            if diff_id:
                diff_url = f"{api.host}/datadiffs/{diff_id}/overview"
                rich.print(f"{diff_url} \n")
            logger.error(error)


def _diff_output_base(dev_path: str, prod_path: str) -> str:
    return f"\n[green]{prod_path} <> {dev_path}[/] \n"
