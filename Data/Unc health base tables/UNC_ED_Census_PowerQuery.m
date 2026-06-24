// =====================================================================
// UNC Health Hospitals - ED Census  (Power Query / M)
// Mimics the Tableau Custom SQL from "UNC Health Hospitals ED Census.tds"
// against the Lakehouse base table:
//     ed_real_time_census_master
//
// HOW TO USE
// 1. Power BI Desktop > Home > Transform data > New Source > Blank Query.
// 2. Advanced Editor > paste this script.
// 3. Edit SqlEndpoint / LakehouseName, then Close & Apply.
//
// Output columns == UNC_Health_Hospitals_ED_Census_DummyData.csv
//   parent_location_name | location_name | current_emergency_department_name |
//   emergency_department_current_care_area | last_update | encounter_count
// =====================================================================
let
    SqlEndpoint   = "your-workspace.datawarehouse.fabric.microsoft.com",
    LakehouseName = "YourLakehouse",

    Source = Sql.Database(SqlEndpoint, LakehouseName),
    ED     = Source{[Schema = "dbo", Item = "ed_real_time_census_master"]}[Data],

    // WHERE is_current = 1 AND current_emergency_department_name LIKE 'EMERG%'
    Filtered = Table.SelectRows(ED, each
        [is_current] = 1
        and Text.StartsWith([current_emergency_department_name], "EMERG")),

    // GROUP BY ... ; MAX(last_warehouse_update_instant) ; COUNT(DISTINCT encounter_epic_csn)
    Grouped = Table.Group(
        Filtered,
        {"parent_location_name", "location_name", "current_emergency_department_name",
         "emergency_department_current_care_area"},
        {
            {"last_update", each List.Max([last_warehouse_update_instant])},
            {"encounter_count", each List.Count(List.Distinct([encounter_epic_csn])), Int64.Type}
        })
in
    Grouped
