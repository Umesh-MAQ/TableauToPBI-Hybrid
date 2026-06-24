/* =====================================================================
   UNC Health Hospitals - ED Census
   Fabric T-SQL translation of the Tableau Custom SQL (from
   "UNC Health Hospitals ED Census.tds").
   ---------------------------------------------------------------------
   Aggregation from the ED real-time census base table. Run against the
   Lakehouse SQL analytics endpoint after the base table
   ed_real_time_census_master has been loaded.

   Output columns match UNC_Health_Hospitals_ED_Census_DummyData.csv exactly.
   ===================================================================== */
SELECT
        parent_location_name
    ,   location_name
    ,   current_emergency_department_name
    ,   emergency_department_current_care_area
    ,   MAX(last_warehouse_update_instant)        AS last_update
    ,   COUNT(DISTINCT encounter_epic_csn)        AS encounter_count
FROM    REPORTING_EHR.ed_real_time_census_master
WHERE   is_current = 1
    AND current_emergency_department_name LIKE 'EMERG%'
GROUP BY
        parent_location_name
    ,   location_name
    ,   current_emergency_department_name
    ,   emergency_department_current_care_area;
