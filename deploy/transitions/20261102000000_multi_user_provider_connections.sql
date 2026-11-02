-- MANUAL, DESTRUCTIVE TRANSITION. Never include in the automatic manifest.
-- Run via reset_provider_backend.py after stopping admissions and draining work.
BEGIN;
DO $$ BEGIN
    IF to_regclass('analysis_backend.schema_revision') IS NOT NULL THEN
        RAISE EXCEPTION 'backend_transition_already_applied';
    END IF;
    IF EXISTS (SELECT 1 FROM analysis_backend.jobs WHERE status IN ('reserved','running')) THEN
        RAISE EXCEPTION 'backend_transition_requires_no_active_jobs';
    END IF;
END $$;
DELETE FROM analysis_backend.memory;
DELETE FROM analysis_backend.jobs;
DELETE FROM analysis_backend.connections;
DELETE FROM analysis_backend.rate_limits;
ALTER TABLE analysis_backend.connections ADD COLUMN name text NOT NULL DEFAULT '';
ALTER TABLE analysis_backend.connections ADD CONSTRAINT analysis_connection_name CHECK (length(name)<=80 AND name !~ '[[:cntrl:]]');
ALTER TABLE analysis_backend.jobs DROP CONSTRAINT analysis_jobs_auth_contract;
ALTER TABLE analysis_backend.jobs DROP COLUMN connection_id;
ALTER TABLE analysis_backend.jobs ADD CONSTRAINT analysis_jobs_auth_type CHECK (auth_type IN ('api_key','personal_chatgpt'));
CREATE TABLE analysis_backend.job_model_bindings (
    user_id uuid NOT NULL,
    job_id uuid NOT NULL,
    tier text NOT NULL CHECK (tier IN ('quick','deep')),
    connection_id uuid NOT NULL,
    PRIMARY KEY(job_id,tier),
    FOREIGN KEY(user_id,job_id) REFERENCES analysis_backend.jobs(user_id,id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,connection_id) REFERENCES analysis_backend.connections(user_id,id) DEFERRABLE INITIALLY DEFERRED
);
ALTER TABLE analysis_backend.job_model_bindings ENABLE ROW LEVEL SECURITY;
CREATE POLICY backend_job_model_bindings ON analysis_backend.job_model_bindings
    TO lucrari_analysis_backend USING (true) WITH CHECK (true);
REVOKE ALL ON analysis_backend.job_model_bindings FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON analysis_backend.job_model_bindings TO lucrari_analysis_backend;
CREATE FUNCTION analysis_backend.check_job_bindings() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid; j analysis_backend.jobs; n integer;
BEGIN
    IF TG_TABLE_NAME='jobs' THEN
        target:=CASE WHEN TG_OP='DELETE' THEN OLD.id ELSE NEW.id END;
    ELSE
        target:=CASE WHEN TG_OP='DELETE' THEN OLD.job_id ELSE NEW.job_id END;
    END IF;
    SELECT * INTO j FROM analysis_backend.jobs WHERE id=target;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT count(*) INTO n FROM analysis_backend.job_model_bindings WHERE job_id=target;
    IF jsonb_typeof(j.model_settings->'quick') IS DISTINCT FROM 'object'
       OR jsonb_typeof(j.model_settings->'deep') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'invalid_model_snapshot' USING ERRCODE='23514';
    END IF;
    IF j.auth_type='personal_chatgpt' THEN
        IF n<>0 OR (j.model_settings->'quick'->>'provider') IS DISTINCT FROM 'chatgpt'
                OR (j.model_settings->'deep'->>'provider') IS DISTINCT FROM 'chatgpt' THEN
            RAISE EXCEPTION 'invalid_personal_bindings' USING ERRCODE='23514';
        END IF;
    ELSIF n<>2 OR EXISTS (
        SELECT 1 FROM analysis_backend.job_model_bindings b
        JOIN analysis_backend.connections c ON (c.user_id,c.id)=(b.user_id,b.connection_id)
        WHERE b.job_id=target AND (c.provider='chatgpt'
            OR c.provider IS DISTINCT FROM j.model_settings->b.tier->>'provider'
            OR jsonb_typeof(j.model_settings->b.tier->'model') IS DISTINCT FROM 'string')
    ) THEN
        RAISE EXCEPTION 'invalid_api_bindings' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION analysis_backend.check_job_bindings() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION analysis_backend.check_job_bindings() TO lucrari_analysis_backend;
CREATE CONSTRAINT TRIGGER check_analysis_job_bindings AFTER INSERT OR UPDATE ON analysis_backend.jobs
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION analysis_backend.check_job_bindings();
CREATE CONSTRAINT TRIGGER check_analysis_binding_rows AFTER INSERT OR UPDATE OR DELETE ON analysis_backend.job_model_bindings
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION analysis_backend.check_job_bindings();
CREATE FUNCTION analysis_backend.immutable_connection_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id,NEW.user_id,NEW.provider) IS DISTINCT FROM (OLD.id,OLD.user_id,OLD.provider) THEN
        RAISE EXCEPTION 'connection_identity_is_immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION analysis_backend.immutable_connection_identity() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION analysis_backend.immutable_connection_identity() TO lucrari_analysis_backend;
CREATE TRIGGER connection_identity_guard BEFORE UPDATE ON analysis_backend.connections
    FOR EACH ROW EXECUTE FUNCTION analysis_backend.immutable_connection_identity();
CREATE FUNCTION analysis_backend.immutable_job_configuration() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id,NEW.user_id,NEW.request_key,NEW.request_hash,NEW.request,NEW.model_settings,NEW.auth_type)
       IS DISTINCT FROM (OLD.id,OLD.user_id,OLD.request_key,OLD.request_hash,OLD.request,OLD.model_settings,OLD.auth_type) THEN
        RAISE EXCEPTION 'job_configuration_is_immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION analysis_backend.immutable_job_configuration() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION analysis_backend.immutable_job_configuration() TO lucrari_analysis_backend;
CREATE TRIGGER job_configuration_guard BEFORE UPDATE ON analysis_backend.jobs
    FOR EACH ROW EXECUTE FUNCTION analysis_backend.immutable_job_configuration();
CREATE FUNCTION analysis_backend.immutable_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' OR EXISTS (SELECT 1 FROM analysis_backend.jobs WHERE id=OLD.job_id) THEN
        RAISE EXCEPTION 'job_binding_is_immutable' USING ERRCODE='23514';
    END IF;
    RETURN OLD;
END $$;
REVOKE ALL ON FUNCTION analysis_backend.immutable_binding() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION analysis_backend.immutable_binding() TO lucrari_analysis_backend;
CREATE TRIGGER binding_update_guard BEFORE UPDATE OR DELETE ON analysis_backend.job_model_bindings
    FOR EACH ROW EXECUTE FUNCTION analysis_backend.immutable_binding();
CREATE TABLE analysis_backend.schema_revision (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision integer NOT NULL CHECK (revision=2)
);
INSERT INTO analysis_backend.schema_revision VALUES (true,2);
ALTER TABLE analysis_backend.schema_revision ENABLE ROW LEVEL SECURITY;
CREATE POLICY backend_schema_revision ON analysis_backend.schema_revision TO lucrari_analysis_backend USING (true);
REVOKE ALL ON analysis_backend.schema_revision FROM PUBLIC,anon,authenticated;
GRANT SELECT ON analysis_backend.schema_revision TO lucrari_analysis_backend;
COMMIT;
