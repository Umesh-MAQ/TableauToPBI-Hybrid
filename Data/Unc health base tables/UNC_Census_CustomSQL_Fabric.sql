/* =====================================================================
   UNC Health Hospitals - Census
   Fabric T-SQL translation of the Tableau Custom SQL (from
   "UNC Health Hospitals Census.tds").
   ---------------------------------------------------------------------
   Single-table projection from the department occupancy base table.
   Run against the Lakehouse SQL analytics endpoint after the base table
   real_time_department_occupancy_statistics_master has been loaded.

   Output columns match UNC_Health_Hospitals_Census_DummyData.csv exactly,
   including EXTRACT_DATETIME (= valid_from_instant).
   ===================================================================== */
SELECT
        extract_date
    ,   department_location_key
    ,   parent_revenue_location_name
    ,   revenue_location_name
    ,   unit_grouping
    ,   department_name
    ,   department_level_of_care_group
    ,   extract_hour
    ,   extract_minute
    ,   valid_from_instant
    ,   valid_to_instant
    ,   is_topofhour
    ,   staffed_beds
    ,   open_beds
    ,   occupied_beds
    ,   unavailable_beds
    ,   staffed_occupancy_percent
    ,   number_incoming
    ,   number_outgoing
    ,   number_expected_open
    ,   licensed_beds
    ,   is_current
    ,   physical_beds
    ,   is_endofday
    ,   valid_from_instant AS EXTRACT_DATETIME
FROM    REPORTING_EHR.real_time_department_occupancy_statistics_master
WHERE   (is_topofhour = 1 OR is_current = 1 OR is_endofday = 1)
    AND extract_date >= CAST('2024-01-01' AS date);
