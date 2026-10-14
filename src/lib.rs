//! Optional computer capability. Meerkat retains model and agent authority.

pub mod backend;
pub mod config;
pub mod credentials;
pub mod domain;
pub mod gateway;
mod machine;
mod store;

pub use config::{
    ComputerKind, ComputerProfile, Config, CredentialConfig, ObservationConfig, ObservationDetail,
    ObservationMode, ObservationSurface, PoolConfig, ServerConfig, TransferLimits,
};
pub use domain::{
    Attachment, AttachmentRequest, Broker, BrokerError, CallRequest, Clock, ComputerOwner,
    ComputerView, CredentialFulfillment, FileDirection, FileMetadata, ForkRequest, ForkTarget,
    ForkView, LastBackendFailure, LeaseView, OperatorAccess, SecureInputDelivery,
    SecureInputTarget, WorkerCallResponse, WorkerClient, WorkerLease,
};
