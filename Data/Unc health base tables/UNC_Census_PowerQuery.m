// =====================================================================
// UNC Health Hospitals - Census  (Power Query / M)
// Mimics the Tableau Custom SQL from "UNC Health Hospitals Census.tds"
// against the Lakehouse base table:
//     real_time_department_occupancy_statistics_master
//
// HOW TO USE
// 1. Power BI Desktop > Home > Transform data > New Source > Blank Query.
// 2. Advanced Editor > paste this script.
// 3. Edit SqlEndpoint / LakehouseName, then Close & Apply.
//
// Output columns == UNC_Health_Hospitals_Census_DummyData.csv (incl. EXTRACT_DATETIME).
// =====================================================================
let
    SqlEndpoint   = "your-workspace.datawarehouse.fabric.microsoft.com",
    LakehouseName = "YourLakehouse",

    Source = Sql.Database(SqlEndpoint, LakehouseName),
    Occ    = Source{[Schema = "dbo", Item = "real_time_department_occupancy_statistics_master"]}[Data],

    // WHERE (is_topofhour=1 OR is_current=1 OR is_endofday=1) AND extract_date >= 2024-01-01
    Filtered = Table.SelectRows(Occ, each
        ([is_topofhour] = 1 or [is_current] = 1 or [is_endofday] = 1)
        and Date.From([extract_date]) >= #date(2024, 1, 1)),

    // valid_from_instant AS EXTRACT_DATETIME
    WithExtractDateTime = Table.AddColumn(Filtered, "EXTRACT_DATETIME", each [valid_from_instant]),

    // Final projection (column order matches the main CSV)
    Result = Table.SelectColumns(WithExtractDateTime, {
        "extract_date", "department_location_key", "parent_revenue_location_name",
        "revenue_location_name", "unit_grouping", "department_name",
        "department_level_of_care_group", "extract_hour", "extract_minute",
        "valid_from_instant", "valid_to_instant", "is_topofhour", "staffed_beds",
        "open_beds", "occupied_beds", "unavailable_beds", "staffed_occupancy_percent",
        "number_incoming", "number_outgoing", "number_expected_open", "licensed_beds",
        "is_current", "physical_beds", "is_endofday", "EXTRACT_DATETIME"
    })
in
    Result
